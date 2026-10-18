"""Read-only git probes must never take optional locks (``GIT_OPTIONAL_LOCKS=0``).

``git status`` opportunistically refreshes the index under ``.git/index.lock``. ``bounded_git_probe``
SIGKILLs the probe's process group on timeout, and a git killed while it holds that lock leaves it
behind for good: every later git write in the repo then fails with "Unable to create index.lock:
File exists". The session-start workspace snapshot (``agent.coding_context``) runs exactly such a
``status`` with a 2.5 s timeout in the session cwd, which stranded locks in a real repo three days
running.

The kill test uses REAL git: the spawned probe's ``communicate`` is wrapped so that the moment git
creates ``index.lock`` its process group is SIGKILLed and the "timeout" fires (or never, if git
exits without taking the lock). That reproduces a timeout landing mid-refresh deterministically
instead of racing a tiny wall-clock timeout.
"""

import os
import shutil
import signal
import subprocess
import time

import pytest

from hermes_cli import _subprocess_compat
from hermes_cli._subprocess_compat import bounded_git_probe

pytestmark = [
    pytest.mark.platforms("posix"),
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

_FILES = 2000


def _git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def stat_dirty_repo(tmp_path):
    """A repo whose every tracked file is stat-dirty, so ``status`` must refresh (and lock) the index."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for i in range(_FILES):
        (repo / f"f{i}").write_text(f"{i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    later = time.time() + 5
    for i in range(_FILES):
        os.utime(repo / f"f{i}", (later, later))
    return repo


def _kill_when_lock_appears(monkeypatch, lock):
    """Make the probe's ``communicate`` time out as soon as git holds *lock*; record whether it did."""
    from hermes_cli.local_runtime import processes

    seen = {"lock": False}
    real_spawn = processes.spawn_server

    def spawn(*args, **kwargs):
        proc, job = real_spawn(*args, **kwargs)
        real_communicate = proc.communicate

        def communicate(input=None, timeout=None):
            deadline = time.monotonic() + 30
            while proc.poll() is None and time.monotonic() < deadline:
                if lock.exists():
                    seen["lock"] = True
                    # Kill while git still holds the lock (what a timeout mid-refresh does); the
                    # probe's own tree-kill then runs on the already-dead group.
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise subprocess.TimeoutExpired(proc.args, timeout or 0)
            return real_communicate(input=input, timeout=timeout)

        proc.communicate = communicate
        return proc, job

    monkeypatch.setattr(processes, "spawn_server", spawn)
    return seen


def test_killed_status_probe_leaves_no_index_lock(stat_dirty_repo, monkeypatch):
    lock = stat_dirty_repo / ".git" / "index.lock"
    seen = _kill_when_lock_appears(monkeypatch, lock)

    bounded_git_probe(["git", "-C", str(stat_dirty_repo), "status", "--porcelain=2", "--branch"], timeout=30)

    assert not lock.exists(), "a killed read-only status probe stranded .git/index.lock"
    assert not seen["lock"], "the read-only status probe took .git/index.lock"


def test_coding_context_status_probe_leaves_no_index_lock(stat_dirty_repo, monkeypatch):
    """The session-start workspace snapshot passes its own (repo-filter) env; it must still not lock."""
    from agent import coding_context

    lock = stat_dirty_repo / ".git" / "index.lock"
    seen = _kill_when_lock_appears(monkeypatch, lock)

    coding_context.build_coding_workspace_block(stat_dirty_repo)

    assert not lock.exists(), "the workspace snapshot's status probe stranded .git/index.lock"
    assert not seen["lock"]


@pytest.mark.parametrize("caller_env", [None, {"PATH": os.environ.get("PATH", ""), "GIT_OPTIONAL_LOCKS": "1"}])
def test_bounded_git_probe_env_disables_optional_locks(monkeypatch, caller_env):
    captured = {}

    def fake_run(argv, *, timeout, env=None, **_kw):
        captured["env"] = dict(env or {})
        return subprocess.CompletedProcess(list(argv), 0, "ok\n", "")

    monkeypatch.setattr(_subprocess_compat, "bounded_probe_run", fake_run)
    assert bounded_git_probe(["git", "status"], timeout=1, env=caller_env) == "ok"
    assert captured["env"].get("GIT_OPTIONAL_LOCKS") == "0"
    assert captured["env"].get("GIT_NO_LAZY_FETCH") == "1"


@pytest.fixture
def status_recorder(tmp_path, monkeypatch):
    """A ``git`` shim first on PATH that logs ``GIT_OPTIONAL_LOCKS`` for each ``status`` call, then
    execs the real git, so every audited caller runs for real and is checked on what git received."""
    real_git = shutil.which("git")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log = tmp_path / "status.log"
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do [ \"$a\" = status ] && "
        f"echo \"${{GIT_OPTIONAL_LOCKS-unset}}\" >> '{log}' && break; done\n"
        f"exec '{real_git}' \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("GIT_OPTIONAL_LOCKS", raising=False)

    def values():
        return log.read_text().split() if log.exists() else []

    return values


def _audited_status_callers(repo):
    from hermes_cli import version_info, web_git, worktree_gc, worktree_ops
    from tools import async_delegation_recovery_hints, subagent_worktree

    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    return {
        "coding_context": lambda: __import__("agent.coding_context", fromlist=["x"]).build_coding_workspace_block(repo),
        "version_info": lambda: version_info._git_version_info(repo),
        "web_git.repo_status": lambda: web_git.repo_status(str(repo)),
        "web_git.file_diff_vs_head": lambda: web_git.file_diff_vs_head(str(repo), "f0"),
        "worktree_gc._dirty_split": lambda: worktree_gc._dirty_split(str(repo)),
        "worktree_ops._worktree_is_dirty": lambda: worktree_ops._worktree_is_dirty(str(repo), str(repo)),
        "async_delegation.git_state_hint": lambda: async_delegation_recovery_hints.git_state_hint(str(repo)),
        "subagent_worktree.finalize": lambda: subagent_worktree.finalize_subagent_worktree(
            {"path": str(repo), "branch": "x", "base_commit": head}, prune=False),
    }


@pytest.mark.parametrize("caller", [
    "coding_context", "version_info", "web_git.repo_status", "web_git.file_diff_vs_head",
    "worktree_gc._dirty_split", "worktree_ops._worktree_is_dirty", "async_delegation.git_state_hint",
    "subagent_worktree.finalize",
])
def test_automatic_status_probes_disable_optional_locks(stat_dirty_repo, status_recorder, caller):
    _audited_status_callers(stat_dirty_repo)[caller]()
    seen = status_recorder()
    assert seen, f"{caller} never ran git status"
    assert set(seen) == {"0"}, f"{caller} ran git status with GIT_OPTIONAL_LOCKS={seen}"
    assert not (stat_dirty_repo / ".git" / "index.lock").exists()

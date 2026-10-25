import importlib.util
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "kanban_workspace_retention.py"
spec = importlib.util.spec_from_file_location("retention", SCRIPT)
retention = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(retention)


def setup(tmp_path, status="done", filename="valuable.txt", mtime_age=20):
    board = tmp_path / "boards" / "test"
    board.mkdir(parents=True)
    db = board / "kanban.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT,status TEXT,workspace_path TEXT,workspace_kind TEXT)")
    workspace = tmp_path / "workspaces" / "task-1"
    workspace.mkdir(parents=True)
    if filename:
        (workspace / filename).write_text("x")
    old = time.time() - mtime_age * 86400
    for p in [workspace, *workspace.rglob("*")]:
        p.touch()
        import os
        os.utime(p, (old, old))
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("task-1", status, str(workspace), "scratch"))
    return workspace, tmp_path / "boards"


def test_archive_terminal_old_nonregenerable(tmp_path):
    _, boards = setup(tmp_path)
    assert retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())[0]["action"] == "archive"


def test_delete_terminal_old_only_regenerable(tmp_path):
    _, boards = setup(tmp_path, filename=".keep")
    row = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())[0]
    assert row["action"] == "delete"
    assert "no unpushed commits" in row["reason"]


def test_keep_nonterminal(tmp_path):
    _, boards = setup(tmp_path, status="running")
    assert retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())[0]["action"] == "keep"


def test_active_worktree_guard(tmp_path):
    workspace, boards = setup(tmp_path)
    row = retention.plan(tmp_path / "workspaces", boards, active_worktrees={workspace})[0]
    assert row["action"] == "keep"
    assert "active git worktree" in row["reason"]


def test_recent_mtime_guard(tmp_path):
    _, boards = setup(tmp_path, mtime_age=2)
    row = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())[0]
    assert row["action"] == "keep"
    assert "idle for" in row["reason"] and "need 3" in row["reason"]


def test_git_probe_nonzero_fails_closed(tmp_path, monkeypatch):
    workspace, boards = setup(tmp_path, filename=".keep")
    gitdir = workspace / ".git"
    gitdir.mkdir()
    import os
    old = time.time() - 20 * 86400
    os.utime(gitdir, (old, old))
    os.utime(workspace, (old, old))
    monkeypatch.setattr(retention.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 128, "", "probe denied"))
    row = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())[0]
    assert row["action"] == "keep"
    assert row["git_probe_error"] and "probe denied" in row["git_probe_error"]
    assert "git probe error" in row["reason"]


def test_git_probe_timeout_fails_closed(tmp_path, monkeypatch):
    workspace, boards = setup(tmp_path, filename=".keep")
    gitdir = workspace / ".git"
    gitdir.mkdir()
    import os
    old = time.time() - 20 * 86400
    os.utime(gitdir, (old, old))
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setattr(retention.subprocess, "run", timeout)
    row = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())[0]
    assert row["action"] == "keep"
    assert "timed out" in row["git_probe_error"]


def test_worktree_inventory_failure_fails_closed(tmp_path, monkeypatch):
    _, boards = setup(tmp_path)
    def fail(_repo):
        raise subprocess.CalledProcessError(1, "git worktree list", stderr="inventory denied")
    monkeypatch.setattr(retention, "_git_worktrees", fail)
    row = retention.plan(tmp_path / "workspaces", boards, repo=tmp_path, active_worktrees=None)[0]
    assert row["action"] == "keep"
    assert "inventory denied" in row["reason"]


def test_candidate_nested_in_active_worktree_fails_closed(tmp_path):
    workspace, boards = setup(tmp_path)
    row = retention.plan(tmp_path / "workspaces", boards,
                         active_worktrees={workspace.parent})[0]
    assert row["action"] == "keep"
    assert "active git worktree" in row["reason"]


def test_root_default_board_is_planned(tmp_path):
    """The default board lives at <home>/kanban.db, not under boards/; its cards must be planned too."""
    _workspace, boards = setup(tmp_path)
    root_db = tmp_path / "kanban.db"
    (boards / "test" / "kanban.db").rename(root_db)
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set(), extra_dbs=[root_db])
    assert [r["task_id"] for r in rows] == ["task-1"]
    assert rows[0]["action"] == "archive"


# ---- t_a74f97ce: apply-mode retention, 3-day idle, git-aware delete/archive, pressure trigger ----
import hashlib
import os
import tarfile

import pytest

DAY = 86400


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def age_tree(ws, days, include_git=False):
    old = time.time() - days * DAY
    for dirpath, dirs, files in os.walk(ws):
        if ".git" in dirs and not include_git:
            dirs.remove(".git")
        for n in [*dirs, *files]:
            os.utime(os.path.join(dirpath, n), (old, old), follow_symlinks=False)
        os.utime(dirpath, (old, old))


def make_repo_workspace(tmp_path, status="done", age=5, push=True, dirty=False):
    """A real clone (workspace) of a bare origin, aged ``age`` days (excluding .git)."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", cwd=seed)
    (seed / "a.txt").write_text("a")
    git("add", ".", cwd=seed)
    git("commit", "-qm", "one", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    boards = tmp_path / "boards"
    (boards / "b").mkdir(parents=True)
    ws = tmp_path / "workspaces" / "task-1"
    ws.parent.mkdir()
    git("clone", "-q", str(origin), str(ws), cwd=tmp_path)
    if not push:
        (ws / "b.txt").write_text("b")
        git("add", ".", cwd=ws)
        git("commit", "-qm", "local only", cwd=ws)
    if dirty:
        (ws / "wip.txt").write_text("wip")
    age_tree(ws, age)
    with sqlite3.connect(boards / "b" / "kanban.db") as conn:
        conn.execute("CREATE TABLE tasks (id TEXT,status TEXT,workspace_path TEXT,workspace_kind TEXT)")
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("task-1", status, str(ws), "scratch"))
    return ws, boards


def plan_one(tmp_path, boards, **kw):
    return retention.plan(tmp_path / "workspaces", boards, active_worktrees=set(), **kw)[0]


def test_clean_pushed_repo_idle_3d_is_deleted_without_archive(tmp_path):
    _, boards = make_repo_workspace(tmp_path, age=3.5)
    row = plan_one(tmp_path, boards)
    assert row["action"] == "delete"
    assert row["dirty"] is False and row["unpushed_commits"] is False


def test_idle_ignores_git_dir_mtime(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    now = time.time()
    os.utime(ws / ".git", (now, now))
    os.utime(ws / ".git" / "HEAD", (now, now))  # `git status` touches .git
    assert plan_one(tmp_path, boards)["action"] == "delete"


def test_under_3d_idle_kept(tmp_path):
    _, boards = make_repo_workspace(tmp_path, age=2.9)
    assert plan_one(tmp_path, boards)["action"] == "keep"


def test_unpushed_repo_is_archived(tmp_path):
    _, boards = make_repo_workspace(tmp_path, age=5, push=False)
    row = plan_one(tmp_path, boards)
    assert row["action"] == "archive" and row["unpushed_commits"] is True


def test_dirty_repo_is_archived(tmp_path):
    _, boards = make_repo_workspace(tmp_path, age=5, dirty=True)
    row = plan_one(tmp_path, boards)
    assert row["action"] == "archive" and row["dirty"] is True


def test_stash_counts_as_dirty(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    (ws / "a.txt").write_text("changed")
    git("stash", "-q", cwd=ws)
    age_tree(ws, 5)
    assert plan_one(tmp_path, boards)["action"] == "archive"


def test_non_git_with_content_is_archived(tmp_path):
    _, boards = setup(tmp_path, filename="notes.md.bak", mtime_age=4)
    row = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())[0]
    assert row["action"] == "archive"


@pytest.mark.parametrize("status", ["running", "ready", "blocked", "scheduled", "triage", "todo", "review"])
def test_never_touches_live_statuses(tmp_path, status):
    _, boards = make_repo_workspace(tmp_path, status=status, age=30)
    assert plan_one(tmp_path, boards)["action"] == "keep"


def test_archived_status_is_eligible(tmp_path):
    _, boards = make_repo_workspace(tmp_path, status="archived", age=5)
    assert plan_one(tmp_path, boards)["action"] == "delete"


def test_idle_days_is_configurable(tmp_path):
    _, boards = make_repo_workspace(tmp_path, age=5)
    assert plan_one(tmp_path, boards, idle_seconds=7 * DAY)["action"] == "keep"


def test_git_status_failure_fails_closed(tmp_path, monkeypatch):
    _, boards = make_repo_workspace(tmp_path, age=5)
    real = subprocess.run
    def fake(cmd, *a, **k):
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "status denied")
        return real(cmd, *a, **k)
    monkeypatch.setattr(retention.subprocess, "run", fake)
    row = plan_one(tmp_path, boards)
    assert row["action"] == "keep" and "status denied" in row["reason"]


def test_apply_archive_writes_tar_and_sha256_then_removes(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5, dirty=True)
    arch = tmp_path / "arch"
    rows = retention.execute(retention.plan(tmp_path / "workspaces", boards, active_worktrees=set()), arch, apply=True)
    assert rows[0]["action"] == "archived" and not ws.exists()
    tar = arch / "task-1.tar"
    assert tar.is_file()
    line = (arch / "task-1.tar.sha256").read_text().split()
    assert line[0] == hashlib.sha256(tar.read_bytes()).hexdigest() and line[1] == "task-1.tar"
    with tarfile.open(tar) as t:
        assert any(n.endswith("wip.txt") for n in t.getnames())
    assert rows[0]["archive"] == str(tar) and rows[0]["archive_sha256"] == line[0]


def test_apply_archive_existing_destination_does_not_fail(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5, dirty=True)
    arch = tmp_path / "arch"
    arch.mkdir()
    (arch / "task-1.tar").write_text("previous run")
    rows = retention.execute(retention.plan(tmp_path / "workspaces", boards, active_worktrees=set()), arch, apply=True)
    assert rows[0]["action"] == "archived" and not ws.exists()
    assert (arch / "task-1.tar").read_text() == "previous run"
    assert Path(rows[0]["archive"]).name != "task-1.tar" and Path(rows[0]["archive"]).is_file()


def test_apply_archive_verification_failure_keeps_source(tmp_path, monkeypatch):
    ws, boards = make_repo_workspace(tmp_path, age=5, dirty=True)
    monkeypatch.setattr(retention, "_verify_tar", lambda *a, **k: False)
    arch = tmp_path / "arch"
    rows = retention.execute(retention.plan(tmp_path / "workspaces", boards, active_worktrees=set()), arch, apply=True)
    assert ws.exists() and rows[0]["action"] == "archive_failed"
    assert not list(arch.glob("*.tar*"))


def test_apply_delete_removes_clean_pushed(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    arch = tmp_path / "arch"
    rows = retention.execute(retention.plan(tmp_path / "workspaces", boards, active_worktrees=set()), arch, apply=True)
    assert rows[0]["action"] == "deleted" and not ws.exists() and not arch.exists()


def test_one_failure_does_not_stop_other_rows(tmp_path, monkeypatch):
    rows = [{"task_id": "a", "source": str(tmp_path / "missing"), "action": "delete", "reason": ""},
            {"task_id": "b", "source": str(tmp_path / "b"), "action": "delete", "reason": ""}]
    (tmp_path / "b").mkdir()
    out = retention.execute(rows, tmp_path / "arch", apply=True)
    assert out[0]["action"] == "delete_failed" and out[1]["action"] == "deleted"


def test_dry_run_changes_nothing(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    retention.execute(retention.plan(tmp_path / "workspaces", boards, active_worktrees=set()), tmp_path / "arch")
    assert ws.exists()


# pressure trigger / schedule gate
def test_decide_pressure_runs_immediately(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"last_full_run_date": "2026-10-25"}')
    now = time.mktime((2026, 10, 25, 14, 0, 0, 0, 0, -1))
    d = retention.decide_run(free_bytes=11 * 10**9, now=now, state_file=state, min_free_bytes=12 * 10**9)
    assert d == "pressure"


def test_decide_daily_slot_once_per_day(tmp_path):
    state = tmp_path / "state.json"
    ok = 50 * 10**9
    before_slot = time.mktime((2026, 10, 25, 2, 0, 0, 0, 0, -1))
    after_slot = time.mktime((2026, 10, 25, 3, 5, 0, 0, 0, -1))
    assert retention.decide_run(ok, before_slot, state, 12 * 10**9) is None
    assert retention.decide_run(ok, after_slot, state, 12 * 10**9) == "daily"
    retention.record_run(state, after_slot)
    assert retention.decide_run(ok, after_slot + 3600, state, 12 * 10**9) is None


def test_decide_pressure_is_throttled(tmp_path):
    """A persistent shortfall must not re-scan every tick forever."""
    state = tmp_path / "state.json"
    now = time.mktime((2026, 10, 25, 14, 0, 0, 0, 0, -1))
    retention.record_run(state, now)
    assert retention.decide_run(1 * 10**9, now + 60, state, 12 * 10**9) is None
    assert retention.decide_run(1 * 10**9, now + 1800, state, 12 * 10**9) == "pressure"


def test_main_auto_idle_prints_nothing(tmp_path, capsys):
    state = tmp_path / "state.json"
    now = time.time()
    retention.record_run(state, now)
    rc = retention.main(["--home", str(tmp_path), "--archive-root", str(tmp_path / "a"), "--apply",
                         "--auto", "--state-file", str(state), "--min-free-gb", "0"])
    assert rc == 0 and capsys.readouterr().out == ""


def test_main_apply_reports_free_space_and_archives(tmp_path, capsys):
    ws, boards = make_repo_workspace(tmp_path, age=5, dirty=True)
    home = tmp_path / "home"
    (home / "kanban").mkdir(parents=True)
    (home / "kanban" / "boards").symlink_to(boards)
    (home / "kanban" / "workspaces").symlink_to(tmp_path / "workspaces")
    rc = retention.main(["--home", str(home), "--archive-root", str(tmp_path / "arch"), "--apply",
                         "--summary", "--min-free-gb", "0"])
    out = capsys.readouterr().out
    assert rc == 0 and not ws.exists()
    assert "free before" in out and "free after" in out and "task-1.tar" in out and "archived" in out


def test_floor_alert_exit_code_when_still_low(tmp_path, capsys):
    _ws, boards = make_repo_workspace(tmp_path, age=5)
    home = tmp_path / "home"
    (home / "kanban").mkdir(parents=True)
    (home / "kanban" / "boards").symlink_to(boards)
    (home / "kanban" / "workspaces").symlink_to(tmp_path / "workspaces")
    rc = retention.main(["--home", str(home), "--archive-root", str(tmp_path / "a"), "--apply", "--summary",
                         "--min-free-gb", "99999999"])
    assert rc == 2 and "ALERT" in capsys.readouterr().err


# ---- review round 1 blockers (B1-B3) and warnings (W1, W2) ----

def test_detached_head_only_commit_is_archived_not_deleted(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    git("checkout", "-q", "--detach", cwd=ws)
    (ws / "c.txt").write_text("detached work")
    git("add", ".", cwd=ws)
    git("commit", "-qm", "detached only", cwd=ws)
    age_tree(ws, 5)
    row = plan_one(tmp_path, boards)
    assert row["action"] == "archive" and row["unpushed_commits"] is True


def test_tag_only_commit_is_archived(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    git("checkout", "-q", "--detach", cwd=ws)
    (ws / "c.txt").write_text("x")
    git("add", ".", cwd=ws)
    git("commit", "-qm", "tagged", cwd=ws)
    git("tag", "keepme", cwd=ws)
    git("checkout", "-q", "main", cwd=ws)
    age_tree(ws, 5)
    assert plan_one(tmp_path, boards)["action"] == "archive"


def test_shared_workspace_with_live_card_is_kept(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("task-2", "running", str(ws), "scratch"))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert len(rows) == 1 and rows[0]["action"] == "keep"
    assert "shared" in rows[0]["reason"]


def test_shared_workspace_all_terminal_is_one_row(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("task-2", "archived", str(ws), "scratch"))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert len(rows) == 1 and rows[0]["action"] == "delete"


def test_shared_workspace_live_card_on_other_board_is_kept(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    (boards / "c").mkdir()
    with sqlite3.connect(boards / "c" / "kanban.db") as c:
        c.execute("CREATE TABLE tasks (id TEXT,status TEXT,workspace_path TEXT,workspace_kind TEXT)")
        c.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("task-9", "blocked", str(ws), "scratch"))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert [r["action"] for r in rows] == ["keep"]


def test_declared_path_outside_workspace_roots_is_kept(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    outside = tmp_path / "home_repo"
    ws.rename(outside)
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("UPDATE tasks SET workspace_path=?", (str(outside),))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert [r["action"] for r in rows] == ["keep"] and "outside" in rows[0]["reason"]


def test_workspace_root_itself_is_never_a_target(tmp_path):
    _ws, boards = make_repo_workspace(tmp_path, age=5)
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("UPDATE tasks SET workspace_path=?", (str(tmp_path / "workspaces"),))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert all(r["action"] == "keep" for r in rows)


def test_board_scoped_workspace_root_is_contained(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    scoped = boards / "b" / "workspaces" / "task-1"
    scoped.parent.mkdir()
    ws.rename(scoped)
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("UPDATE tasks SET workspace_path=?", (str(scoped),))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert [r["action"] for r in rows] == ["delete"]


def test_ignored_env_file_in_clean_repo_is_archived(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    (ws / ".git" / "info" / "exclude").write_text("secret.env\n")
    (ws / "secret.env").write_text("x")
    age_tree(ws, 5)
    assert plan_one(tmp_path, boards)["action"] == "archive"


def test_ignored_cache_dir_alone_is_still_deleted(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    (ws / ".git" / "info" / "exclude").write_text("node_modules/\n")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "x.js").write_text("x")
    age_tree(ws, 5)
    assert plan_one(tmp_path, boards)["action"] == "delete"


def test_card_reopened_between_plan_and_apply_is_not_removed(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert rows[0]["action"] == "delete"
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("UPDATE tasks SET status='ready'")
    out = retention.execute(rows, tmp_path / "arch", apply=True)
    assert out[0]["action"] == "keep" and ws.exists()


def test_live_dir_kind_card_on_same_path_keeps_scratch_workspace(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("task-dir", "running", str(ws), "dir"))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert [r["action"] for r in rows] == ["keep"]
    # and execute() re-checks that card too
    for kind in ("worktree", "dir"):
        retention.execute(rows, tmp_path / "arch", apply=True)
    assert ws.exists()


def test_live_card_nested_inside_done_workspace_keeps_parent(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("task-in", "running", str(ws / "sub"), "dir"))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert [r["action"] for r in rows] == ["keep"]


def test_live_card_on_ancestor_keeps_child(tmp_path):
    _ws, boards = make_repo_workspace(tmp_path, age=5)
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("task-up", "ready", str(tmp_path / "workspaces"), "dir"))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert [r["action"] for r in rows] == ["keep"]


def test_reopened_dir_kind_card_between_plan_and_apply_keeps(tmp_path):
    ws, boards = make_repo_workspace(tmp_path, age=5)
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("task-dir", "done", str(ws), "dir"))
    rows = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())
    assert rows[0]["action"] == "delete"
    with sqlite3.connect(boards / "b" / "kanban.db") as c:
        c.execute("UPDATE tasks SET status='running' WHERE id='task-dir'")
    retention.execute(rows, tmp_path / "arch", apply=True)
    assert ws.exists()


def _low_space_run(tmp_path, capsys, free_bytes, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _ws, boards = make_repo_workspace(tmp_path, age=5)
    home = tmp_path / "home"
    (home / "kanban").mkdir(parents=True, exist_ok=True)
    if not (home / "kanban" / "boards").exists():
        (home / "kanban" / "boards").symlink_to(boards)
        (home / "kanban" / "workspaces").symlink_to(tmp_path / "workspaces")
    usage = type(shutil.disk_usage("/"))(total=10**11, used=10**11 - free_bytes, free=free_bytes)
    monkeypatch.setattr(retention.shutil, "disk_usage", lambda _p: usage)
    rc = retention.main(["--home", str(home), "--archive-root", str(tmp_path / "a"), "--apply", "--summary",
                         "--report-dir", str(tmp_path / "reports")])
    return rc, capsys.readouterr()


def test_alert_signature_stable_across_free_space_figures(tmp_path, capsys, monkeypatch):
    rc1, o1 = _low_space_run(tmp_path / "x", capsys, 10_980_000_000, monkeypatch)
    rc2, o2 = _low_space_run(tmp_path / "y", capsys, 11_420_000_000, monkeypatch)
    assert rc1 == rc2 == 2
    assert o1.err[:200] == o2.err[:200]
    assert o1.err.startswith("kanban retention: / below pressure floor")
    assert "10.98" in o1.err and "11.42" in o2.err and "10.98" not in o1.err[:200]
    assert o1.out == "" and o2.out == ""  # bulky JSONL goes to the report file, not stdout


def test_report_dir_holds_jsonl_plan(tmp_path, capsys, monkeypatch):
    _rc, _o = _low_space_run(tmp_path, capsys, 10_000_000_000, monkeypatch)
    files = list((tmp_path / "reports").glob("retention-*.jsonl"))
    assert len(files) == 1 and '"task_id"' in files[0].read_text()

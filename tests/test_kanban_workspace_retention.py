import importlib.util
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
    _, boards = setup(tmp_path, mtime_age=3)
    row = retention.plan(tmp_path / "workspaces", boards, active_worktrees=set())[0]
    assert row["action"] == "keep"
    assert "within 14 days" in row["reason"]


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

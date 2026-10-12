"""An explicit unblock resumes work on the PR the operator referenced."""
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()


def _blocked_pr_card(conn):
    tid = kb.create_task(conn, title="resume PR", assignee="dev")
    assert kb.block_task(conn, tid, reason="waiting")
    kb.add_comment(conn, tid, "operator", "Resume on https://github.com/example/project/pull/42")
    return tid


def test_explicit_unblock_after_pr_comment_spawns(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "dev")
    with kbc.connect() as conn:
        tid = _blocked_pr_card(conn)
        assert kb.unblock_task(conn, tid)
        # Guarantee strict ordering even on a one-second clock boundary.
        conn.execute("UPDATE task_events SET created_at = created_at + 2 WHERE task_id = ? AND kind = 'unblocked'", (tid,))
        result = kbd.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 12345, reconcile_orphans=False)
    assert any(row[0] == tid for row in result.spawned)


def test_reclaim_after_pr_comment_stays_guarded(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="guard PR", assignee="dev")
        kb.add_comment(conn, tid, "operator", "PR https://github.com/example/project/pull/42")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "reclaimed", None)
        conn.execute("UPDATE task_events SET created_at = created_at + 2 WHERE task_id = ? AND kind = 'reclaimed'", (tid,))
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_no_resume_event_after_pr_comment_stays_guarded(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="guard PR", assignee="dev")
        kb.add_comment(conn, tid, "operator", "PR https://github.com/example/project/pull/42")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"

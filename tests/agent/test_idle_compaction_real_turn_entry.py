"""Idle compaction must see the idle gap through the REAL turn entry path.

``AIAgent.run_conversation`` admits a durable turn lease whose ``start()`` stamps
``_last_activity_ts`` (so the liveness watchdog doesn't measure from the previous turn),
the gateway's cached-agent reset does the same, and a freshly spawned process (CLI
``--resume``, gateway restart) initialises it to now. All of that happens BEFORE the turn
prologue, so an idle clock based only on ``_last_activity_ts`` sees a ~0 s gap and never
fires. The clock is the durable timestamp of the newest assistant/tool row instead.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_state import SessionDB

from agent.turn_context_compaction import _idle_gap_seconds
from tests.agent.test_compression_concurrent_fork import _build_agent_with_db


class _Stop(Exception):
    pass


def _seed(db: SessionDB, sid: str, *, assistant_age_s: float) -> None:
    db.create_session(sid, source="cli")
    db.append_message(sid, "user", "earlier")
    db.append_message(sid, "assistant", "reply")
    ts = time.time() - assistant_age_s
    with db._lock:  # noqa: SLF001 — test fixture backdates the row
        db._conn.execute(  # noqa: SLF001
            "UPDATE messages SET timestamp = ? WHERE session_id = ?", (ts, sid)
        )
        db._conn.commit()  # noqa: SLF001


def test_idle_gap_survives_turn_entry(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_ENTRY"
    _seed(db, sid, assistant_age_s=3600)
    # A freshly built agent: _last_activity_ts == now, exactly like `--resume` / gateway restart.
    agent = _build_agent_with_db(db, sid, stub_compressor=False)
    agent.compression_enabled = True
    agent.compression_idle_compact_after_seconds = 60

    seen = {}

    def _probe(agent_, out, *a, **k):
        seen["gap"] = _idle_gap_seconds(agent_)
        raise _Stop()

    with patch("agent.turn_context_compaction._idle_compaction", _probe):
        try:
            agent.run_conversation("hello", conversation_history=[
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "reply"},
            ])
        except _Stop:
            pass

    assert seen, "turn prologue never reached the idle check"
    assert seen["gap"] >= 3500, f"idle gap lost at turn entry: {seen['gap']:.1f}s"


def test_recent_assistant_row_means_not_idle(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    _seed(db, "WARM", assistant_age_s=5)
    agent = SimpleNamespace(_session_db=db, session_id="WARM", _last_activity_ts=time.time() - 9999)
    assert _idle_gap_seconds(agent) < 60


def test_falls_back_to_in_memory_clock_without_db_rows(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("EMPTY", source="cli")
    agent = SimpleNamespace(_session_db=db, session_id="EMPTY", _last_activity_ts=time.time() - 120)
    assert 119 <= _idle_gap_seconds(agent) < 130
    no_db = SimpleNamespace(_session_db=None, session_id="X", _last_activity_ts=time.time() - 300)
    assert 299 <= _idle_gap_seconds(no_db) < 310


def test_db_errors_fall_back_quietly() -> None:
    class _Broken:
        def latest_model_activity_ts(self, _sid):
            raise RuntimeError("locked")

    agent = SimpleNamespace(_session_db=_Broken(), session_id="S", _last_activity_ts=time.time() - 200)
    assert 199 <= _idle_gap_seconds(agent) < 210

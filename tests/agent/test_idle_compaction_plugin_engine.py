"""Idle compaction must honour the ContextEngine contract, not ContextCompressor internals.

A plugin engine (e.g. hermes-lcm's LCMEngine) subclasses ``ContextEngine`` and only
implements the abstract surface. The idle trigger used to read
``summary_target_ratio`` straight off the engine, so enabling
``compression.idle_compact_after_seconds`` with ``context.engine: <plugin>`` raised
``AttributeError`` in the turn prologue. These tests pin the contract: the ABC carries
neutral defaults for every attribute the idle/preflight paths read, and a minimal
plugin engine is compacted after an idle gap like the built-in compressor.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_state import SessionDB

from agent.context_engine import ContextEngine

from tests.agent.test_compression_concurrent_fork import _build_agent_with_db
from tests.agent.test_idle_compaction_lock_and_guards import _history, _run_prologue


class _MinimalEngine(ContextEngine):
    """Only the abstract surface — the shape a third-party plugin ships."""

    def __init__(self) -> None:
        self.threshold_tokens = 100_000
        self.context_length = 200_000
        self.compress_calls: List[int] = []

    @property
    def name(self) -> str:
        return "minimal"

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        pass

    def should_compress(self, prompt_tokens: int = None) -> bool:
        return False

    def compress(self, messages, current_tokens: Optional[int] = None, focus_topic=None,
                 force: bool = False, memory_context: str = "") -> List[Dict[str, Any]]:
        self.compress_calls.append(len(messages))
        return [{"role": "user", "content": "[CONTEXT COMPACTION] summary"}] + messages[-2:]


def test_context_engine_abc_provides_idle_contract_defaults() -> None:
    engine = _MinimalEngine()
    assert engine.summary_target_ratio == 0.20
    assert engine.last_compression_rough_tokens == 0
    assert engine.awaiting_real_usage_after_compression is False
    assert engine.last_real_prompt_tokens == 0
    assert engine.get_active_compression_failure_cooldown() is None
    assert engine.get_active_compression_failure_cooldown(refresh=True) is None


def _prep(db: SessionDB, sid: str, engine: Any):
    agent = _build_agent_with_db(db, sid, stub_compressor=False)
    agent.context_compressor = engine
    agent.compression_enabled = True
    agent.compression_idle_compact_after_seconds = 60
    agent._last_activity_ts = time.time() - 3600
    agent._cached_system_prompt = "SYSTEM"
    return agent


def test_idle_compaction_fires_for_minimal_plugin_engine(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_PLUGIN"
    db.create_session(sid, source="cli")
    engine = _MinimalEngine()
    agent = _prep(db, sid, engine)

    ctx = _run_prologue(agent, _history(), rough_tokens=50_000)

    assert len(engine.compress_calls) == 1
    assert any("[CONTEXT COMPACTION]" in str(m.get("content")) for m in ctx.messages)


def test_idle_compaction_skips_small_transcript_for_plugin_engine(tmp_path: Path) -> None:
    """Default ratio 0.20 of 100k => 20k floor; a 10k transcript is left alone."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_PLUGIN_SMALL"
    db.create_session(sid, source="cli")
    engine = _MinimalEngine()
    agent = _prep(db, sid, engine)

    _run_prologue(agent, _history(), rough_tokens=10_000)

    assert engine.compress_calls == []


def test_idle_compaction_tolerates_engine_without_abc_defaults(tmp_path: Path) -> None:
    """Duck-typed engines (no ABC defaults, e.g. older plugins) must not crash the prologue."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_DUCK"
    db.create_session(sid, source="cli")
    engine = _MinimalEngine()
    agent = _prep(db, sid, engine)

    class _Duck:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, item):
            if item in {"summary_target_ratio", "last_compression_rough_tokens",
                        "awaiting_real_usage_after_compression",
                        "get_active_compression_failure_cooldown"}:
                raise AttributeError(item)
            return getattr(self._inner, item)

    agent.context_compressor = _Duck(engine)
    _run_prologue(agent, _history(), rough_tokens=50_000)
    assert len(engine.compress_calls) == 1

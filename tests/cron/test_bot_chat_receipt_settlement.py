"""A Bot Chat cron delivery booked ``queued`` settles once the owner settles its receipt.

Bot Chat admission returns a durable receipt without waiting for the owner, so the run books
``delivery_queued`` / ledger ``queued``. Only a re-delivery used to move that on, so a job that
delivers once per run (a script-only ``no_agent`` job takes no queue, no canonical completion)
stayed ``queued`` forever although the owner answered.
"""
import pytest

from cron import executions, jobs, scheduler, scheduler_tick
from tests.cron.test_cron_live_bot_delivery import _FakeAuthority, _owner
from tools import bot_live_delivery as mailbox


def _run_bot_chat_job(tmp_path, monkeypatch, **create):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("_HERMES_CRON_EXTERNAL_WORKER", raising=False)
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: _owner(tmp_path))
    authority = _FakeAuthority()
    monkeypatch.setattr(mailbox, "authority_delivery", authority)
    monkeypatch.setattr(scheduler, "run_job", lambda job, **kw: (True, "raw", "the result", None))
    job = jobs.create_job(schedule="every 1h", deliver="bot-chat", **create)
    scheduler.run_one_job(job)
    scheduler.drain_delivery_queue({}, None)
    return job, authority


def _tick_housekeeping():
    scheduler_tick._pre_dispatch_housekeeping(scheduler, headless=True)


def _state(job):
    return jobs.get_job(job["id"]), executions.latest_execution(job["id"])


@pytest.fixture
def run(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "wake.py").write_text("print('x')\n")
    job, authority = _run_bot_chat_job(tmp_path, monkeypatch, no_agent=True, script="wake.py", prompt=None)
    saved, row = _state(job)
    assert saved["last_status"] == "delivery_queued" and saved["last_delivery_queued"]
    assert row["delivery_outcome"] == "queued"
    return job, authority


def test_settled_receipt_settles_job_and_ledger(run):
    job, authority = run
    (key,) = authority.records
    authority.publish(key, status="settled", reply="done")

    _tick_housekeeping()

    saved, row = _state(job)
    assert saved["last_delivery_queued"] is None
    assert (saved["last_status"], saved["last_delivery_error"]) == ("ok", None)
    assert row["delivery_outcome"] == "delivered"


def test_pending_receipt_stays_queued(run):
    job, _authority = run
    _tick_housekeeping()
    saved, row = _state(job)
    assert saved["last_delivery_queued"] and saved["last_status"] == "delivery_queued"
    assert row["delivery_outcome"] == "queued"


@pytest.mark.parametrize("status", ["failed", "cancelled", "ambiguous"])
def test_unsuccessful_receipt_surfaces_a_delivery_failure(run, status):
    job, authority = run
    (key,) = authority.records
    authority.publish(key, status=status, error="owner refused")

    _tick_housekeeping()

    saved, row = _state(job)
    assert saved["last_delivery_queued"] is None
    assert saved["last_status"] == "delivery_failed"
    assert "owner refused" in saved["last_delivery_error"]
    assert row["delivery_outcome"] == "failed"


def test_job_record_is_not_rewritten_when_it_names_another_delivery(run, monkeypatch):
    job, authority = run
    (key,) = authority.records
    authority.publish(key, status="settled", reply="done")
    jobs.update_job(job["id"], {"last_delivery_queued": {"bot-chat:(own)": {
        "status": "queued", "delivery_id": "f" * 64}}})

    _tick_housekeeping()

    saved, row = _state(job)
    # The ledger row settles from its own receipt; the job record names another delivery, so stays.
    assert saved["last_status"] == "delivery_queued" and saved["last_delivery_queued"]
    assert row["delivery_outcome"] == "delivered"


def test_earlier_run_settles_after_a_later_run_started(run, monkeypatch):
    """A receipt that settles after the next run began must still settle its own ledger row."""
    job, authority = run
    (key1,) = authority.records
    first = executions.latest_execution(job["id"])
    # Run 2 starts while receipt 1 is still pending.
    scheduler.run_one_job(jobs.get_job(job["id"]))
    scheduler.drain_delivery_queue({}, None)
    second = executions.latest_execution(job["id"])
    assert second["id"] != first["id"]
    (key2,) = [k for k in authority.records if k != key1]
    authority.publish(key1, status="settled", reply="one")
    authority.publish(key2, status="failed", error="owner refused")

    _tick_housekeeping()

    assert executions.get_execution(first["id"])["delivery_outcome"] == "delivered"
    assert executions.get_execution(second["id"])["delivery_outcome"] == "failed"
    saved = jobs.get_job(job["id"])
    assert saved["last_status"] == "delivery_failed"
    assert "owner refused" in saved["last_delivery_error"]


def test_earlier_run_settles_even_when_latest_is_pending(run):
    job, authority = run
    (key1,) = authority.records
    first = executions.latest_execution(job["id"])
    scheduler.run_one_job(jobs.get_job(job["id"]))
    scheduler.drain_delivery_queue({}, None)
    second = executions.latest_execution(job["id"])
    authority.publish(key1, status="settled", reply="one")

    _tick_housekeeping()

    assert executions.get_execution(first["id"])["delivery_outcome"] == "delivered"
    assert executions.get_execution(second["id"])["delivery_outcome"] == "queued"
    assert jobs.get_job(job["id"])["last_status"] == "delivery_queued"

"""Write a queued cron delivery's terminal outcome back onto the run that queued it.

An agent run hands its send to ``cron.delivery_queue`` and books ``last_status =
"delivery_queued"`` / ledger ``delivery_outcome = "queued"``. Once a gateway drain sends,
refuses, suppresses or fences the send ``unknown``, the job record and the execution
ledger must carry the real result (main's inline ``ok``/``delivered`` and
``delivery_failed``/``failed`` + ``last_delivery_error``); otherwise ``hermes cron list``
says "delivery still in progress" forever and refused sends never surface.

Both sides call :func:`settle_queued_delivery`: the drain right after it terminalizes a
row, and the run right after its own bookkeeping (a drain can finish before the run's
``mark_job_run`` lands). Every write is fenced on the execution id, so the call is
idempotent and never rewrites a later run's status.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Queue terminal status -> ledger delivery_outcome. ``unknown`` (owner died mid-send, never
# retried) is surfaced as a failure: the user cannot be told the result arrived.
_LEDGER_OUTCOME = {
    "delivered": "delivered", "suppressed": "suppressed", "failed": "failed", "unknown": "failed",
}


def _settle_job_record(job_id: str, execution_id: str, delivery_error: Optional[str]) -> bool:
    """Stamp the outcome onto the job only while *execution_id* is still its current run:
    the ledger's latest attempt for the job AND the job's latest recorded completion."""
    from cron.executions import latest_execution
    from cron.jobs import _with_job, save_jobs

    latest = latest_execution(job_id)
    if latest is None or latest.get("id") != execution_id:
        return False

    def apply(jobs, _i, job):
        completions = job.get("canonical_completions") or []
        if not completions or completions[-1] != execution_id:
            return False
        status = job.get("last_status")
        if status == "delivery_queued":
            status = "delivery_failed" if delivery_error else "ok"
        if (status, delivery_error) == (job.get("last_status"), job.get("last_delivery_error")):
            return True
        job["last_status"] = status
        job["last_delivery_error"] = delivery_error
        save_jobs(jobs)
        return True

    return bool(_with_job(job_id, apply, missing=False))


def _terminal(execution_id: str) -> Optional[tuple[str, Optional[str]]]:
    """``(ledger_outcome, delivery_error)`` once the queue row is terminal, else None."""
    from cron.delivery_queue import get_status

    row = get_status(str(execution_id))
    if not row or row["status"] not in _LEDGER_OUTCOME:
        return None
    delivery_error = None
    if row["status"] in ("failed", "unknown"):
        delivery_error = str(row.get("error") or f"delivery {row['status']}")
    return _LEDGER_OUTCOME[row["status"]], delivery_error


def settled_outcome(execution_id: str) -> Optional[str]:
    """The ledger outcome a drain already reached for *execution_id* (None while queued)."""
    terminal = _terminal(execution_id)
    return terminal[0] if terminal else None


def settle_queued_delivery(job_id: Optional[str], execution_id: Optional[str]) -> bool:
    """Apply the queue's terminal outcome for *execution_id*; False while still in flight."""
    if not job_id or not execution_id:
        return False
    terminal = _terminal(str(execution_id))
    if terminal is None:
        return False
    from cron.executions import settle_delivery_outcome

    settle_delivery_outcome(str(execution_id), terminal[0])
    _settle_job_record(str(job_id), str(execution_id), terminal[1])
    return True


def settle_quietly(job_id: Optional[str], execution_id: Optional[str]) -> None:
    """Bookkeeping failure must not undo a terminal at-most-once send or fail its run."""
    try:
        settle_queued_delivery(job_id, execution_id)
    except Exception:
        logger.warning("Cron delivery %s: outcome write-back failed", execution_id, exc_info=True)


_RECEIPT_OUTCOME = {"settled": "delivered", "failed": "failed", "cancelled": "failed", "ambiguous": "failed"}


def _bot_chat_receipt_home(target: str):
    """Profile home a ``bot-chat:<profile>`` receipt lives in (``(own)`` is this profile)."""
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_hermes_home

    name = target.split(":", 1)[1]
    return (get_hermes_home() if name == "(own)" else get_profile_dir(name)).resolve()


def settle_bot_chat_receipts() -> int:
    """Book the owner's terminal receipt onto a run that left Bot Chat delivery ``queued``.

    Bot Chat admission returns a durable receipt without waiting for the owner, so the run
    books ``delivery_queued``/``queued`` and no cron queue row exists to drain; the receipt
    file is the only record of the outcome. Fenced on the job's latest ledger attempt, still
    ``queued``, whose delivery id (derived from that execution id) is the one the job recorded,
    so a later run is never rewritten. Pending receipts are left for a later tick.
    """
    import hashlib
    import json

    from cron.executions import latest_execution, settle_delivery_outcome
    from cron.jobs import _with_job, load_jobs, save_jobs
    from hermes_constants import get_hermes_home
    from tools.bot_live_delivery import read_delivery_result

    source_home = get_hermes_home().resolve()
    settled = 0
    for job in load_jobs():
        queued = job.get("last_delivery_queued") or {}
        targets = {t: r for t, r in queued.items() if str(t).startswith("bot-chat:")}
        if not targets or job.get("last_status") != "delivery_queued":
            continue
        job_id = job["id"]
        try:
            latest = latest_execution(job_id)
            if latest is None or latest.get("delivery_outcome") != "queued":
                continue
            results = []
            for target, receipt in targets.items():
                home = _bot_chat_receipt_home(target)
                key = hashlib.sha256(json.dumps(
                    [str(source_home), job_id, str(latest["id"]), str(home)],
                    ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
                if receipt.get("delivery_id") != key:
                    break  # the recorded receipt belongs to some other run
                record = read_delivery_result(home, key)
                if record is None or record.get("status") not in _RECEIPT_OUTCOME:
                    break  # still in flight (or unreadable): a later tick decides
                results.append((target, record))
            else:
                failed = [(t, r) for t, r in results if _RECEIPT_OUTCOME[r["status"]] == "failed"]
                error = "; ".join(
                    f"{t} {r['status']}: {r.get('error') or r.get('reason') or 'not completed'}"
                    for t, r in failed) or None

                def apply(jobs, _i, current, _id=latest["id"], _error=error):
                    if current.get("last_status") != "delivery_queued" or not current.get("last_delivery_queued"):
                        return False
                    current["last_status"] = "delivery_failed" if _error else "ok"
                    current["last_delivery_error"] = _error
                    current["last_delivery_queued"] = None
                    save_jobs(jobs)
                    return True

                if _with_job(job_id, apply, missing=False):
                    settle_delivery_outcome(str(latest["id"]), "failed" if error else "delivered")
                    settled += 1
        except Exception:
            logger.warning("Cron job %s: Bot Chat receipt settlement failed", job_id, exc_info=True)
    return settled

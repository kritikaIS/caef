"""Safety Rollback Protocol — the last line of defense (SAFETY_PROTOCOL.md §5).

**No LLM is reachable from this module.** Nothing here imports the Agent, the
RAG store, Guard Rail or the Sandbox. If the model is degraded, unavailable, or
is itself the source of the repeated failures, a safety path that still calls it
is not a safety path (NFR-4). This is pure deterministic logic over the History
Table and the A/B partition record.

The rollback redeploy also deliberately bypasses Guard Rail and Sandbox: the
artifact it restores is by definition already-verified — it passed both gates
before it was ever deployed. Re-verifying it would make recovery depend on the
same machinery that may have just failed (§5.3).

One implementation, two triggers (§7):
  - automatic, on the 3rd strike for a device
  - manual, from the operator's Frontend button
Both call `rollback()`. There is no second copy.
"""

import logging

import config
from server.db.models import Device, Event, HistoryRecord, SessionLocal, aware, utcnow
from server.deploy import deployer
from server.schemas import RecordStatus, RecordType

log = logging.getLogger("caef.rollback")


class RollbackUnavailable(RuntimeError):
    """No known-good artifact exists to roll back to.

    Operator-escalation case, explicitly outside automated remediation for v0.1
    (LOOPS.md §5 failure mode). Raised rather than silently doing nothing, so it
    cannot be mistaken for a successful rollback.
    """


def record_strike(device_id: str, reason: str) -> int:
    """Count one strike against a device and return the new total.

    Both trigger sources feed this same counter, per PRD OQ-2's resolution
    (SAFETY_PROTOCOL.md §5): retry-budget exhaustion and on-device crashes tied
    to a recent deploy. Scoped per device, unlike the retry budget, which is per
    event (§4).
    """
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        if device is None:
            device = Device(id=device_id, mcu_type="unknown")
            db.add(device)
        device.strike_count += 1
        count = device.strike_count
        db.commit()
    log.warning("strike %s/%s for %s: %s", count, config.STRIKE_LIMIT, device_id, reason)
    return count


def crash_is_attributable(device_id: str, event: Event) -> bool:
    """Does this crash count as a strike against a recent deploy?

    Only crashes within `CRASH_ATTRIBUTION_WINDOW_SECONDS` of a deploy count
    (§5). A device that crashes days later is reporting a new problem, not
    evidence that the deployed patch was bad — counting it would eventually
    roll back firmware that has been running fine.
    """
    with SessionLocal() as db:
        live = (
            db.query(HistoryRecord)
            .filter(
                HistoryRecord.device_id == device_id,
                HistoryRecord.status == RecordStatus.DEPLOYED,
            )
            .order_by(HistoryRecord.deployed_at.desc())
            .first()
        )
        if live is None:
            return False
        # A crash reported by firmware other than the one we deployed is not
        # attributable to that deploy.
        if event.current_state_hash != live.fw_hash:
            return False
        age = (utcnow() - aware(live.deployed_at)).total_seconds()
        return age <= config.CRASH_ATTRIBUTION_WINDOW_SECONDS


def should_roll_back(device_id: str) -> bool:
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        return bool(device and device.strike_count >= config.STRIKE_LIMIT)


def generation_halted(device_id: str) -> bool:
    """Whether autonomous generation is halted for this device.

    Checked before the Agent is ever invoked. Persisted in the DB rather than
    held in memory, because §5.1 forbids silently auto-resuming — a server
    restart must not clear a halt.
    """
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        return bool(device and device.generation_halted)


def rollback(
    device_id: str,
    reason: str,
    event_id: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> str:
    """Halt generation, restore the last known-good artifact, record it.

    The single rollback implementation (§7): the operator's manual button and
    the automatic 3-strikes path both land here.
    """
    # Halt unconditionally and first (§5.1). Stopping generation is the part
    # that must never fail: a device with nothing to roll back to is exactly the
    # device that must not receive another generated patch. A rollback that
    # raises below still leaves generation stopped.
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        if device is None:
            device = Device(id=device_id, mcu_type="unknown")
            db.add(device)
        device.generation_halted = True
        db.commit()

    fw = deployer.last_known_good(device_id)
    if fw is None:
        raise RollbackUnavailable(
            f"no known-good firmware recorded for {device_id}; generation halted, "
            "operator escalation required"
        )
    code = deployer.load_firmware(fw)
    if code is None:
        raise RollbackUnavailable(
            f"known-good hash {fw} for {device_id} is not in the firmware store; "
            "generation halted, operator escalation required"
        )

    log.warning("ROLLBACK %s to %s: %s", device_id, fw, reason)
    # Straight to deploy: no Agent, no Guard Rail, no Sandbox (§5.3).
    record_id, ack = deployer.deploy(
        device_id,
        code,
        RecordType.ROLLBACK,
        event_id=event_id,
        host=host,
        port=port,
    )
    log.warning(
        "rollback recorded %s (device %s)", record_id, ack.status if ack else "unreachable"
    )
    return record_id


def clear_strikes(device_id: str) -> None:
    """Operator acknowledgment: reset the counter and resume generation.

    Explicitly manual (§5.5). Nothing in the pipeline calls this on its own.
    """
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        if device:
            device.strike_count = 0
            device.generation_halted = False
            db.commit()
    log.info("strikes cleared for %s by operator", device_id)


def revert(device_id: str, event_id: str | None = None, host: str | None = None,
           port: int | None = None) -> str | None:
    """Reversion Sub-Loop body (LOOPS.md §2a): restore the pre-morph firmware.

    Distinct from rollback: a reversion is the *planned* end of a temporary
    morph, not a failure response. It does not halt generation and does not
    touch the strike counter.
    """
    fw = pre_morph_hash(device_id)
    if fw is None:
        log.info("no pre-morph firmware recorded for %s; nothing to revert", device_id)
        return None
    code = deployer.load_firmware(fw)
    if code is None:
        log.warning("pre-morph hash %s missing from the firmware store", fw)
        return None

    log.info("reverting %s to pre-morph %s", device_id, fw)
    record_id, _ = deployer.deploy(
        device_id, code, RecordType.REVERSION, event_id=event_id, host=host, port=port
    )
    return record_id


def pre_morph_hash(device_id: str) -> str | None:
    """The firmware that was live immediately before the current morph.

    Read off the ledger rather than the A/B slot, because a reversion must
    restore what this specific morph replaced, not merely "something older".
    """
    with SessionLocal() as db:
        rows = (
            db.query(HistoryRecord)
            .filter(HistoryRecord.device_id == device_id)
            .order_by(HistoryRecord.deployed_at.desc(), HistoryRecord.id.desc())
            .all()
        )
        current = next((row for row in rows if row.status == RecordStatus.DEPLOYED), None)
        if current is None or current.record_type is not RecordType.MORPH_DEPLOY:
            return None
        for row in rows:
            if row.deployed_at <= current.deployed_at and row.fw_hash != current.fw_hash:
                return row.fw_hash
        return None

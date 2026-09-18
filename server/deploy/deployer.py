"""Deploy — staged rollout, OTA push, History Table writes (TDD.md §2.7).

The path a verified artifact takes to a device:

    Soft Firmware   stage 1: sandbox-passed artifact written to the firmware store
    Soft Firmware 2 stage 2: recorded on the device's inactive A/B slot, ready to push
    OTA push        stage 3: sent to the watchdog, which verifies the hash itself
    History write   stage 4: append-only ledger row, FKEY-linked to device/event/patch

Both staging checkpoints are kept in v0.1 (SAFETY_PROTOCOL.md §1 layer 5). The
A/B slot flip is what makes rollback cheap: the previous active hash becomes the
inactive slot and is never overwritten, so last-known-good is always a real
artifact on disk needing no regeneration (§6).

`fw_hash` is the artifact's filename in the store, so a rollback to any prior
hash is a file read, not a re-generation.
"""

import logging
import socket
from pathlib import Path

import config
from server.db.models import Device, Event, HistoryRecord, Patch, SessionLocal, utcnow
from server.schemas import (
    AgentOutput,
    GuardRailResult,
    OTAAck,
    OTAPush,
    RecordStatus,
    RecordType,
    SandboxResult,
    fw_hash,
)

log = logging.getLogger("caef.deploy")


def _reindex(device_id: str) -> None:
    """Refresh the Agent's corpus after the running firmware changed.

    Imported here, not at module scope: the RAG layer reads the driver library
    and the History Table, both of which sit above Deploy in the dependency
    order. Failures are logged, never raised — the artifact is already live and
    on the ledger by this point, and a stale corpus is a worse next generation,
    not a failed deploy.
    """
    from server.agent.rag import indexer

    try:
        indexer.reindex(device_id)
    except Exception:
        log.exception("corpus refresh failed for %s; next generation may lack precedent", device_id)


# --- firmware store (Soft Firmware) ------------------------------------------


def store_path(fw: str) -> Path:
    return Path(config.FIRMWARE_STORE_DIR) / f"{fw}.py"


def stage_soft_firmware(code: str) -> str:
    """Stage 1. Content-addressed, so staging the same artifact twice is a no-op
    and any historical hash stays retrievable for rollback."""
    Path(config.FIRMWARE_STORE_DIR).mkdir(parents=True, exist_ok=True)
    fw = fw_hash(code)
    path = store_path(fw)
    if not path.exists():
        path.write_text(code)
    return fw


def load_firmware(fw: str) -> str | None:
    path = store_path(fw)
    return path.read_text() if path.exists() else None


# --- persistence -------------------------------------------------------------


def record_patch(
    output: AgentOutput,
    attempt: int,
    verdict: GuardRailResult,
    sandbox: SandboxResult | None,
) -> str:
    """Persist the full audit trail for one attempt (NFR-5): the plan, the code,
    the tool-call trace, and both verdicts. Stored even for rejected attempts —
    a rejection is evidence the safety layers worked, not noise."""
    with SessionLocal() as db:
        patch = Patch(
            id=output.patch_id,
            event_id=output.event_id,
            device_id=output.device_id,
            plan=output.plan,
            target_file=output.target_file,
            code=output.code,
            pins_referenced=output.pins_referenced,
            tool_calls=[call.model_dump() for call in output.tool_calls],
            fw_hash=fw_hash(output.code),
            attempt=attempt,
            guardrail_status=verdict.status,
            guardrail_reason=verdict.reason,
            sandbox_status=sandbox.status if sandbox else None,
            sandbox_logs=sandbox.logs if sandbox else None,
            sandbox_runtime_seconds=sandbox.runtime_seconds if sandbox else None,
        )
        db.merge(patch)
        db.commit()
        return patch.id


def write_history(
    device_id: str,
    fw: str,
    record_type: RecordType,
    event_id: str | None = None,
    patch_id: str | None = None,
    time: int | None = None,
    status: RecordStatus = RecordStatus.DEPLOYED,
) -> str:
    """Append a ledger row and supersede the device's previous live row.

    Append-only (TRD.md §5): superseding edits the *old* row's status, never its
    hash or its links, so the chain stays intact.
    """
    with SessionLocal() as db:
        if status is RecordStatus.DEPLOYED:
            previous = (
                db.query(HistoryRecord)
                .filter(
                    HistoryRecord.device_id == device_id,
                    HistoryRecord.status == RecordStatus.DEPLOYED,
                )
                .all()
            )
            for row in previous:
                row.status = RecordStatus.SUPERSEDED

        device = db.get(Device, device_id)
        if time is None and event_id:
            event = db.get(Event, event_id)
            time = event.timestamp if event else 0

        record = HistoryRecord(
            time=time or 0,  # device-authoritative when an event supplied it
            device_id=device_id,
            event_id=event_id,
            patch_id=patch_id,
            fw_hash=fw,
            record_type=record_type,
            status=status,
            poll_id=device.last_poll_id if device else None,
        )
        db.add(record)
        db.commit()
        return record.id


def promote_to_inactive_slot(device_id: str, fw: str) -> None:
    """Stage 2 ("Soft Firmware 2"): the artifact is recorded on the device's
    inactive A/B slot and marked as what the device *should* be running.

    The active slot is untouched until the device confirms the new firmware is
    live, so a push lost in flight leaves the ledger honest.
    """
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        if device is None:
            device = Device(id=device_id, mcu_type="unknown")
            db.add(device)
        device.inactive_fw_hash = fw
        device.assigned_fw_hash = fw
        db.commit()


def confirm_active(device_id: str, fw: str) -> None:
    """The device reported it is running `fw`. Flip the A/B slots.

    The outgoing active hash becomes inactive and is deliberately *not* dropped:
    it is the last-known-good artifact the Safety Rollback Protocol reaches for
    (SAFETY_PROTOCOL.md §6).
    """
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        if device is None or device.active_fw_hash == fw:
            return
        if device.active_fw_hash and device.active_fw_hash != fw:
            device.inactive_fw_hash = device.active_fw_hash
        device.active_fw_hash = fw
        db.commit()


# --- OTA push ----------------------------------------------------------------


def push_ota(device_id: str, code: str, record_type: RecordType, patch_id: str | None = None,
             target_file: str = "main.py", host: str | None = None,
             port: int | None = None) -> OTAAck | None:
    """Send the artifact to the device watchdog, which verifies the hash itself.

    Returns None if the device is unreachable. A missed push is not an error
    condition: the device's poll loop reconciles against `assigned_fw_hash` and
    re-requests the artifact (LOOPS.md §3).
    """
    push = OTAPush(
        device_id=device_id,
        fw_hash=fw_hash(code),
        target_file=target_file,
        code=code,
        patch_id=patch_id,
        record_type=record_type,
    )
    address = (host or config.DEVICE_OTA_HOST, port or config.OTA_PORT)
    try:
        with socket.create_connection(address, timeout=config.TELEMETRY_TIMEOUT_SECONDS) as sock:
            sock.sendall(push.model_dump_json().encode() + b"\n")
            reply = sock.makefile("r").readline()
        return OTAAck.model_validate_json(reply) if reply.strip() else None
    except (OSError, ValueError) as exc:
        log.warning("OTA push to %s failed: %s; poll loop will reconcile", device_id, exc)
        return None


# --- the deploy path ---------------------------------------------------------


def deploy(
    device_id: str,
    code: str,
    record_type: RecordType,
    event_id: str | None = None,
    patch_id: str | None = None,
    target_file: str = "main.py",
    host: str | None = None,
    port: int | None = None,
) -> tuple[str, OTAAck | None]:
    """Stage, promote, push, record. The single deploy path.

    Used by morph, patch, reversion and rollback alike — one implementation, so
    a rollback cannot diverge from a normal deploy (SAFETY_PROTOCOL.md §7).
    """
    fw = stage_soft_firmware(code)  # Soft Firmware
    promote_to_inactive_slot(device_id, fw)  # Soft Firmware 2
    ack = push_ota(device_id, code, record_type, patch_id, target_file, host, port)
    record_id = write_history(
        device_id=device_id,
        fw=fw,
        record_type=record_type,
        event_id=event_id,
        patch_id=patch_id,
    )
    if ack and ack.status == "accepted":
        confirm_active(device_id, fw)
        # A deployed artifact is precedent: it is what the next generation
        # retrieves as "what worked last time" (DATA_SCHEMAS.md §8 history docs).
        # Here rather than in the Orchestrator because this is the single deploy
        # path (§7) — reversions and rollbacks change the running firmware too,
        # and the corpus has to follow the device, not just the happy path.
        _reindex(device_id)
    log.info(
        "deployed %s fw=%s to %s (ack=%s)",
        record_type,
        fw,
        device_id,
        ack.status if ack else "unreachable",
    )
    return record_id, ack


def last_known_good(device_id: str) -> str | None:
    """The artifact the Safety Rollback Protocol reaches for.

    Pure History Table + A/B slot lookup, no LLM anywhere in this path (NFR-4).
    Prefers the inactive slot — SAFETY_PROTOCOL.md §6 says booting the inactive
    partition is the fast, reliable path over re-pushing bytes.
    """
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        # Only trust the inactive slot once the device has confirmed something
        # newer is live. Until then the slot holds the *candidate* promoted by
        # `promote_to_inactive_slot`, not a known-good artifact — a push lost in
        # flight would otherwise roll the device back onto the firmware it is
        # being rolled back from (SAFETY_PROTOCOL.md §6: the slot is known-good
        # only after the flip in `confirm_active`).
        if device and device.inactive_fw_hash and device.inactive_fw_hash != device.assigned_fw_hash:
            return device.inactive_fw_hash

        # Fall back to the ledger: the most recent deployed artifact that is not
        # the one currently running.
        rows = (
            db.query(HistoryRecord)
            .filter(HistoryRecord.device_id == device_id)
            .order_by(HistoryRecord.deployed_at.desc())
            .all()
        )
        # Roll away from what the server last *intended* the device to run, not
        # from what it is running: on a lost push those differ, and the running
        # firmware is then the known-good one we want back.
        current = (device.assigned_fw_hash or device.active_fw_hash) if device else None
        for row in rows:
            if row.fw_hash != current and row.record_type in (
                RecordType.PATCH_DEPLOY,
                RecordType.REVERSION,
                RecordType.ROLLBACK,
            ):
                return row.fw_hash
        return None

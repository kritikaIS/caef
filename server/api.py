"""Device-facing HTTP API: the server half of the Poll/Reconciliation Loop.

LOOPS.md §3: the device polls at a fixed interval, independent of OTA pushes,
and asks what firmware it is *supposed* to be running. If its running hash
differs from the assigned one, it re-requests the artifact directly. That is the
missed-push safety net — a push dropped by a network blip or an offline device
is corrected here rather than stranding the device on stale firmware.

Both endpoints are read-only with respect to firmware: they serve artifacts that
already passed Guard Rail and Sandbox on their way into the store. Nothing here
can introduce code that skipped a gate (SAFETY_PROTOCOL.md §1).

The operator dashboard mounts onto this same app (TDD.md §2.9), so `API_PORT`
serves both, matching the single port config.py already documents.
"""

import logging
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from server.db.models import Device, HistoryRecord, SessionLocal
from server.deploy import deployer
from server.schemas import OTAPush, PollResponse, RecordStatus, RecordType

log = logging.getLogger("caef.api")

app = FastAPI(title="CAEF", docs_url=None, redoc_url=None)


@app.get("/poll", response_model=PollResponse)
def poll(id: str, current_state_hash: str) -> PollResponse:
    """DATA_SCHEMAS.md §6b. `poll_id` is minted per poll and stored on the
    device, so the next History row written for it carries the poll that
    preceded it (`history.poll_id`, §7)."""
    poll_id = str(uuid.uuid4())
    with SessionLocal() as db:
        device = db.get(Device, id)
        if device is None:
            # An unregistered device gets an honest answer rather than a 404: it
            # has nothing assigned, so it is trivially in sync. The Listener is
            # what registers a device, on its first telemetry.
            return PollResponse(
                poll_id=poll_id, device_id=id, assigned_fw_hash=None, in_sync=True
            )
        device.last_poll_id = poll_id
        assigned = device.assigned_fw_hash
        db.commit()

    return PollResponse(
        poll_id=poll_id,
        device_id=id,
        assigned_fw_hash=assigned,
        in_sync=assigned is None or assigned == current_state_hash,
    )


@app.get("/firmware")
def firmware(id: str) -> JSONResponse:
    """Missed-push recovery: hand back the assigned artifact as an OTA Push
    Payload, byte-identical in shape to what `push_ota` sends, so the watchdog
    runs the same hash check on it (DATA_SCHEMAS.md §6b)."""
    with SessionLocal() as db:
        device = db.get(Device, id)
        assigned = device.assigned_fw_hash if device else None
        if assigned is None:
            raise HTTPException(404, f"no firmware assigned to {id}")
        # The ledger row for this artifact carries the audit links the push
        # payload should keep (NFR-5): which patch produced it, and whether it
        # was a morph, a patch or a rollback.
        record = (
            db.query(HistoryRecord)
            .filter(HistoryRecord.device_id == id, HistoryRecord.fw_hash == assigned)
            .order_by(HistoryRecord.deployed_at.desc())
            .first()
        )

    code = deployer.load_firmware(assigned)
    if code is None:
        # The store is the only source of deployable bytes; regenerating here
        # would mean shipping code that never passed a gate.
        raise HTTPException(410, f"assigned hash {assigned} is not in the firmware store")

    push = OTAPush(
        device_id=id,
        fw_hash=assigned,
        target_file="main.py",
        code=code,
        patch_id=record.patch_id if record else None,
        record_type=record.record_type if record else RecordType.PATCH_DEPLOY,
    )
    log.info("serving %s to %s on re-request", assigned, id)
    return JSONResponse(push.model_dump(mode="json"))


def device_rows() -> list[dict]:
    """Device list + the live ledger row for each, for the dashboard."""
    with SessionLocal() as db:
        devices = db.query(Device).order_by(Device.id).all()
        live = {
            row.device_id: row
            for row in db.query(HistoryRecord)
            .filter(HistoryRecord.status == RecordStatus.DEPLOYED)
            .all()
        }
        return [
            {
                "id": device.id,
                "mcu_type": device.mcu_type,
                "active_fw_hash": device.active_fw_hash,
                "assigned_fw_hash": device.assigned_fw_hash,
                "inactive_fw_hash": device.inactive_fw_hash,
                "in_sync": device.assigned_fw_hash == device.active_fw_hash,
                "strike_count": device.strike_count,
                "generation_halted": device.generation_halted,
                "last_poll_id": device.last_poll_id,
                "live_record": live.get(device.id),
            }
            for device in devices
        ]


def history_rows(device_id: str, limit: int = 50) -> list[HistoryRecord]:
    with SessionLocal() as db:
        return (
            db.query(HistoryRecord)
            .filter(HistoryRecord.device_id == device_id)
            .order_by(HistoryRecord.deployed_at.desc())
            .limit(limit)
            .all()
        )

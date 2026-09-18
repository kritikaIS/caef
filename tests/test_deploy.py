"""M7 check: staged rollout, A/B slot discipline, and an FKEY-traceable ledger.

The audit claim (PRD G5 / NFR-5) is proven here mechanically: from a History
Table row alone, reach the deployed code, the plan that produced it, and the
event that triggered it.
"""

import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from server.db import models as m  # noqa: E402
from server.deploy import deployer  # noqa: E402
from server.schemas import (  # noqa: E402
    AgentOutput,
    GuardRailChecks,
    GuardRailResult,
    RecordStatus,
    RecordType,
    SandboxResult,
    ToolCallRecord,
    TriggerType,
    fw_hash,
)

DEVICE = "pi_node_alpha"
V1 = "print('[firmware] v1', flush=True)\n"
V2 = "print('[firmware] v2', flush=True)\n"
V3 = "print('[firmware] v3', flush=True)\n"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRMWARE_STORE_DIR", tmp_path / "store")
    m.init_db()
    with m.SessionLocal() as db:
        for table in (m.HistoryRecord, m.Patch, m.Event, m.Device):
            db.query(table).delete()
        db.add(m.Device(id=DEVICE, mcu_type="RaspberryPi_4B", active_fw_hash=fw_hash(V1)))
        db.commit()
    yield


def make_event(event="HIGH_HEAT_DETECTED", trigger=TriggerType.CONTEXT_TRIGGER) -> str:
    with m.SessionLocal() as db:
        row = m.Event(
            device_id=DEVICE,
            trigger_type=trigger,
            event=event,
            timestamp=171542000,
            current_state_hash=fw_hash(V1),
            data={"temp_c": 85.4},
        )
        db.add(row)
        db.commit()
        return row.id


def make_output(event_id: str, code: str = V2) -> AgentOutput:
    return AgentOutput(
        patch_id="patch-1",
        event_id=event_id,
        device_id=DEVICE,
        plan="Enable Relay_Fan on GPIO_27; drop Lidar_X2 to free CPU.",
        target_file="main.py",
        code=code,
        pins_referenced=[27],
        tool_calls=[
            ToolCallRecord(
                tool="check_hardware_schema",
                args={"pin_number": 27},
                result="SAFE: Connected to Relay_Fan",
            )
        ],
    )


def passing() -> GuardRailResult:
    return GuardRailResult(
        patch_id="patch-1",
        status="pass",
        checks=GuardRailChecks(
            forbidden_pin_check="pass",
            tool_call_provenance="pass",
            schema_conformance="pass",
            static_safety_denylist="pass",
            current_draw_sanity="pass",
        ),
    )


class FakeWatchdog:
    """Accepts OTA pushes over a real socket, like the device watchdog does."""

    def __init__(self, accept: bool = True) -> None:
        self.port = free_port()
        self.accept = accept
        self.received: list[str] = []
        self.stop = threading.Event()

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen(4)
        self.sock.settimeout(0.5)
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def _serve(self):
        from server.schemas import OTAPush

        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                raw = conn.makefile("r").readline()
                push = OTAPush.model_validate_json(raw)
                self.received.append(push.fw_hash)
                status = "accepted" if self.accept else "rejected"
                conn.sendall(
                    f'{{"device_id":"{push.device_id}","status":"{status}",'
                    f'"fw_hash":"{push.fw_hash}","reason":null}}\n'.encode()
                )

    def __exit__(self, *exc):
        self.stop.set()
        self.sock.close()


# --- staging -----------------------------------------------------------------


def test_soft_firmware_is_content_addressed():
    """Stage 1: any historical hash stays retrievable, so a rollback is a file
    read rather than a regeneration."""
    fw = deployer.stage_soft_firmware(V2)
    assert fw == fw_hash(V2)
    assert deployer.load_firmware(fw) == V2
    assert deployer.stage_soft_firmware(V2) == fw  # idempotent
    assert deployer.load_firmware("nonexistent") is None


def test_promote_marks_the_inactive_slot_not_the_active_one():
    """Stage 2: the active slot only moves once the device confirms."""
    deployer.promote_to_inactive_slot(DEVICE, fw_hash(V2))
    with m.SessionLocal() as db:
        device = db.get(m.Device, DEVICE)
        assert device.assigned_fw_hash == fw_hash(V2)
        assert device.inactive_fw_hash == fw_hash(V2)
        assert device.active_fw_hash == fw_hash(V1)  # unchanged until confirmed


def test_confirm_flips_slots_and_keeps_the_old_artifact():
    """SAFETY_PROTOCOL.md §6: last-known-good is never overwritten."""
    deployer.confirm_active(DEVICE, fw_hash(V2))
    with m.SessionLocal() as db:
        device = db.get(m.Device, DEVICE)
        assert device.active_fw_hash == fw_hash(V2)
        assert device.inactive_fw_hash == fw_hash(V1)  # the outgoing version


# --- audit trail -------------------------------------------------------------


def test_rejected_attempts_are_recorded_too():
    """A rejection is evidence the safety layers worked, not noise."""
    event_id = make_event()
    rejected = GuardRailResult(
        patch_id="patch-1",
        status="fail",
        checks=GuardRailChecks(
            forbidden_pin_check="fail",
            tool_call_provenance="pass",
            schema_conformance="pass",
            static_safety_denylist="pass",
            current_draw_sanity="pass",
        ),
        reason="forbidden pin GPIO_0 referenced in generated code",
    )
    deployer.record_patch(make_output(event_id), attempt=1, verdict=rejected, sandbox=None)
    with m.SessionLocal() as db:
        patch = db.get(m.Patch, "patch-1")
        assert patch.guardrail_status == "fail"
        assert "GPIO_0" in patch.guardrail_reason
        assert patch.sandbox_status is None  # never executed


def test_patch_row_captures_the_full_trace():
    event_id = make_event()
    sandbox = SandboxResult(
        patch_id="patch-1", status="pass", runtime_seconds=10.0, exit_code=0, logs="[firmware] ok"
    )
    deployer.record_patch(make_output(event_id), attempt=1, verdict=passing(), sandbox=sandbox)
    with m.SessionLocal() as db:
        patch = db.get(m.Patch, "patch-1")
        assert patch.plan.startswith("Enable Relay_Fan")
        assert patch.pins_referenced == [27]
        assert patch.tool_calls[0]["args"]["pin_number"] == 27
        assert patch.sandbox_runtime_seconds == 10.0


def test_deployed_artifact_is_traceable_end_to_end():
    """PRD G5 / NFR-5: from a ledger row alone, reach code, plan and event."""
    event_id = make_event()
    deployer.record_patch(make_output(event_id), 1, passing(), None)
    record_id, _ = deployer.deploy(
        DEVICE, V2, RecordType.MORPH_DEPLOY, event_id=event_id, patch_id="patch-1"
    )

    with m.SessionLocal() as db:
        row = db.get(m.HistoryRecord, record_id)
        assert row.fw_hash == fw_hash(V2)
        assert row.time == 171542000  # device-authoritative, not server time
        patch = db.get(m.Patch, row.patch_id)
        event = db.get(m.Event, row.event_id)
        assert patch.plan.startswith("Enable Relay_Fan")
        assert event.event == "HIGH_HEAT_DETECTED"
        assert row.device.id == DEVICE
    # ...and the exact bytes that were deployed are still retrievable.
    assert deployer.load_firmware(row.fw_hash) == V2


def test_new_deploy_supersedes_the_previous_live_row():
    """Exactly one row per device is `deployed` at a time."""
    event_id = make_event()
    deployer.deploy(DEVICE, V2, RecordType.MORPH_DEPLOY, event_id=event_id)
    deployer.deploy(DEVICE, V3, RecordType.PATCH_DEPLOY, event_id=event_id)
    with m.SessionLocal() as db:
        live = db.query(m.HistoryRecord).filter_by(status=RecordStatus.DEPLOYED).all()
        assert len(live) == 1
        assert live[0].fw_hash == fw_hash(V3)
        # Append-only: the superseded row keeps its hash and its links.
        old = db.query(m.HistoryRecord).filter_by(status=RecordStatus.SUPERSEDED).one()
        assert old.fw_hash == fw_hash(V2)
        assert old.event_id == event_id


def test_rollback_rows_need_no_patch():
    """DATA_SCHEMAS.md §7: a rollback references a prior artifact, not new code."""
    record_id = deployer.write_history(DEVICE, fw_hash(V1), RecordType.ROLLBACK)
    with m.SessionLocal() as db:
        assert db.get(m.HistoryRecord, record_id).patch_id is None


# --- OTA ---------------------------------------------------------------------


def test_deploy_pushes_and_confirms_on_accept(monkeypatch):
    with FakeWatchdog() as watchdog:
        event_id = make_event()
        _, ack = deployer.deploy(
            DEVICE, V2, RecordType.MORPH_DEPLOY, event_id=event_id, port=watchdog.port
        )
        assert ack.status == "accepted"
        assert watchdog.received == [fw_hash(V2)]
    with m.SessionLocal() as db:
        assert db.get(m.Device, DEVICE).active_fw_hash == fw_hash(V2)


def test_rejected_push_does_not_flip_the_active_slot():
    """A device that refused the artifact is still running the old one; the
    ledger must not claim otherwise."""
    with FakeWatchdog(accept=False) as watchdog:
        event_id = make_event()
        _, ack = deployer.deploy(
            DEVICE, V2, RecordType.MORPH_DEPLOY, event_id=event_id, port=watchdog.port
        )
        assert ack.status == "rejected"
    with m.SessionLocal() as db:
        device = db.get(m.Device, DEVICE)
        assert device.active_fw_hash == fw_hash(V1)
        assert device.assigned_fw_hash == fw_hash(V2)  # poll loop will reconcile


def test_unreachable_device_still_records_the_assignment(monkeypatch):
    """LOOPS.md §3: a missed push is not an error, it is what polling is for."""
    monkeypatch.setattr(config, "OTA_PORT", free_port())  # nothing bound
    monkeypatch.setattr(config, "TELEMETRY_TIMEOUT_SECONDS", 1)
    event_id = make_event()
    record_id, ack = deployer.deploy(DEVICE, V2, RecordType.MORPH_DEPLOY, event_id=event_id)
    assert ack is None
    with m.SessionLocal() as db:
        assert db.get(m.Device, DEVICE).assigned_fw_hash == fw_hash(V2)
        assert db.get(m.HistoryRecord, record_id).fw_hash == fw_hash(V2)
        assert db.get(m.Device, DEVICE).active_fw_hash == fw_hash(V1)  # not confirmed


# --- last known good ---------------------------------------------------------


def test_last_known_good_prefers_the_inactive_slot():
    """§6: booting the inactive partition beats re-pushing bytes."""
    deployer.confirm_active(DEVICE, fw_hash(V2))
    assert deployer.last_known_good(DEVICE) == fw_hash(V1)


def test_last_known_good_ignores_an_unconfirmed_candidate():
    """§6: the inactive slot is known-good only *after* the flip.

    Between promote and the device's ack it holds the candidate. If that push is
    lost — the exact situation a rollback responds to — trusting the slot would
    restore the firmware being rolled back from.
    """
    deployer.write_history(DEVICE, fw_hash(V1), RecordType.PATCH_DEPLOY)
    deployer.promote_to_inactive_slot(DEVICE, fw_hash(V2))  # pushed, never acked
    assert deployer.last_known_good(DEVICE) == fw_hash(V1)


def test_last_known_good_never_calls_an_llm(monkeypatch):
    """NFR-4: pure History Table + A/B lookup, reachable with the Agent down."""
    import server.agent.agent as agent_module

    def explode(*args, **kwargs):
        raise AssertionError("rollback lookup must not touch the LLM")

    monkeypatch.setattr(agent_module, "build_llm", explode)
    deployer.confirm_active(DEVICE, fw_hash(V2))
    assert deployer.last_known_good(DEVICE) == fw_hash(V1)


def test_last_known_good_falls_back_to_the_ledger():
    """A device with no inactive slot recorded still has a ledger to read."""
    with m.SessionLocal() as db:
        device = db.get(m.Device, DEVICE)
        device.inactive_fw_hash = None
        device.active_fw_hash = fw_hash(V3)
        db.commit()
    deployer.write_history(DEVICE, fw_hash(V2), RecordType.PATCH_DEPLOY)
    deployer.write_history(DEVICE, fw_hash(V3), RecordType.PATCH_DEPLOY)
    assert deployer.last_known_good(DEVICE) == fw_hash(V2)


def test_last_known_good_is_none_for_a_fresh_device():
    with m.SessionLocal() as db:
        db.add(m.Device(id="fresh_device", mcu_type="RaspberryPi_4B"))
        db.commit()
    assert deployer.last_known_good("fresh_device") is None

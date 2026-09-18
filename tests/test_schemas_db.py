"""M1 check: DATA_SCHEMAS.md shapes round-trip, and the FKEY chain
event -> patch -> history is traversable (PRD G5 / NFR-5)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import schemas as s  # noqa: E402
from server.db import models as m  # noqa: E402


def test_hardware_schema_loads_and_denylists():
    hw = s.load_hardware_schema("dev01")
    assert hw.device_id == "pi_node_alpha"
    assert hw.pin(27).connected_device == "Relay_Fan"
    assert hw.pin(27).status is s.PinStatus.DORMANT
    assert hw.is_forbidden(0) and hw.is_forbidden(1)  # constraints.forbidden_pins
    assert not hw.is_forbidden(27)
    assert hw.pin(99) is None


def test_telemetry_payload_both_trigger_types():
    heat = s.TelemetryPayload.model_validate(
        {
            "id": "pi_node_alpha",
            "timestamp": 171542000,
            "trigger_type": "CONTEXT_TRIGGER",
            "event": "HIGH_HEAT_DETECTED",
            "data": {"temp_c": 85.4, "threshold": 80.0},
            "current_state_hash": "a1b2c3d4",
        }
    )
    assert heat.trigger_type is s.TriggerType.CONTEXT_TRIGGER

    crash = s.TelemetryPayload.model_validate(
        {
            "id": "pi_node_alpha",
            "timestamp": 171542100,
            "trigger_type": "CRITICAL_FAILURE",
            "event": "UNHANDLED_EXCEPTION",
            "data": {"trace": "IndexError: list index out of range"},
            "current_state_hash": "a1b2c3d4",
        }
    )
    assert "IndexError" in crash.data["trace"]


def test_agent_output_and_results_shapes():
    out = s.AgentOutput(
        patch_id="p1",
        event_id="e1",
        device_id="pi_node_alpha",
        plan="Enable Relay_Fan on GPIO_27.",
        target_file="main.py",
        code="print('hi')",
        pins_referenced=[27],
        tool_calls=[
            s.ToolCallRecord(
                tool="check_hardware_schema",
                args={"pin_number": 27},
                result="SAFE: Connected to Relay_Fan",
            )
        ],
    )
    assert out.tool_calls[0].args["pin_number"] == 27

    fail = s.SandboxResult(
        patch_id="p1",
        status="fail",
        runtime_seconds=2,
        exit_code=1,
        logs="Traceback ...",
        results="Process exited with code 1 after 2s",
        delta_firmware="- old\n+ new",
    )
    assert fail.delta_firmware  # FAIL(Results, ΔFirmware) is never discarded


def test_fkey_chain_traversable():
    m.init_db()
    with m.SessionLocal() as db:
        dev = m.Device(id="pi_node_alpha", mcu_type="RaspberryPi_4B")
        ev = m.Event(
            device_id=dev.id,
            trigger_type=s.TriggerType.CONTEXT_TRIGGER,
            event="HIGH_HEAT_DETECTED",
            timestamp=171542000,
            current_state_hash="a1b2c3d4",
            data={"temp_c": 85.4},
        )
        db.add_all([dev, ev])
        db.flush()
        patch = m.Patch(
            event_id=ev.id,
            device_id=dev.id,
            plan="Enable fan.",
            target_file="main.py",
            code="print('fan')",
            pins_referenced=[27],
            fw_hash="deadbeef",
        )
        db.add(patch)
        db.flush()
        db.add(
            m.HistoryRecord(
                time=ev.timestamp,
                device_id=dev.id,
                event_id=ev.id,
                patch_id=patch.id,
                fw_hash=patch.fw_hash,
                record_type=s.RecordType.MORPH_DEPLOY,
                status=s.RecordStatus.DEPLOYED,
            )
        )
        db.commit()

        row = db.query(m.HistoryRecord).one()
        # Ledger row -> event -> plan that produced the deployed hash.
        assert row.device.id == "pi_node_alpha"
        assert db.get(m.Patch, row.patch_id).plan == "Enable fan."
        assert db.get(m.Event, row.event_id).event == "HIGH_HEAT_DETECTED"


def test_history_row_allows_null_patch_for_rollback():
    """Rollback/reversion rows reference a prior artifact, not new code.

    The device is provisioned because `HistoryRecord.device_id` is a real FKEY:
    SQLite leaves foreign keys unenforced by default, so an orphan row only
    fails on Postgres.
    """
    with m.SessionLocal() as db:
        db.add(m.Device(id="pi_node_alpha", mcu_type="RaspberryPi_4B"))
        db.flush()
        db.add(
            m.HistoryRecord(
                time=171542500,
                device_id="pi_node_alpha",
                fw_hash="deadbeef",
                record_type=s.RecordType.ROLLBACK,
                status=s.RecordStatus.DEPLOYED,
            )
        )
        db.commit()
        assert db.query(m.HistoryRecord).filter_by(record_type=s.RecordType.ROLLBACK).one()

"""M8 wiring check: the Task handler that joins Agent, Deploy and Rollback.

Covers the asymmetry that defines the two loops (a morph schedules its own
reversion, an auto-patch does not) and the ordering guarantees the Safety
Rollback Protocol depends on. No live LLM (TDD.md §5).
"""

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from server.agent.agent import Agent  # noqa: E402
from server.db import models as m  # noqa: E402
from server.deploy import deployer, rollback  # noqa: E402
from server.orchestrator import Orchestrator  # noqa: E402
from server.schemas import (  # noqa: E402
    AgentTask,
    RecordType,
    SandboxResult,
    TriggerType,
    fw_hash,
)
from tests.fakes import FakeLLM, FakeSandbox, code_reply, tool_call  # noqa: E402

DEVICE = "pi_node_alpha"
BASE = "print('[firmware] baseline', flush=True)\n"
GENERATED = "print('[firmware] generated', flush=True)\n"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRMWARE_STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(config, "OTA_PORT", free_port())  # nothing bound: push fails
    monkeypatch.setattr(config, "TELEMETRY_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(config, "REVERSION_MODE", "time")
    monkeypatch.setattr(config, "REVERSION_WINDOW_SECONDS", 30)  # never fires in-test
    yield


def provision() -> None:
    deployer.stage_soft_firmware(BASE)
    with m.SessionLocal() as db:
        db.add(m.Device(id=DEVICE, mcu_type="RaspberryPi_4B", active_fw_hash=fw_hash(BASE)))
        db.commit()
    deployer.write_history(DEVICE, fw_hash(BASE), RecordType.PATCH_DEPLOY)


def make_task(trigger=TriggerType.CONTEXT_TRIGGER, event="HIGH_HEAT_DETECTED") -> AgentTask:
    with m.SessionLocal() as db:
        row = m.Event(
            device_id=DEVICE,
            trigger_type=trigger,
            event=event,
            timestamp=171542000,
            current_state_hash=fw_hash(BASE),
            data={"temp_c": 85.4},
        )
        db.add(row)
        db.commit()
        event_id = row.id
    return AgentTask(
        task_id="t1",
        event_id=event_id,
        device_id=DEVICE,
        trigger_type=trigger,
        event=event,
        raw_payload={"data": {"temp_c": 85.4}},
    )


def working_agent(code: str = GENERATED) -> Agent:
    """An Agent scripted to check pin 27, then emit passing code."""
    llm = FakeLLM(
        [
            code_reply("Enable the fan on GPIO 27.", code, [27]),
        ]
    )
    sandbox = FakeSandbox(
        [SandboxResult(patch_id="x", status="pass", runtime_seconds=1, exit_code=0)]
    )
    return Agent(llm, sandbox=sandbox)


def failing_agent() -> Agent:
    """Sandbox rejects every attempt, so the retry budget is exhausted."""
    llm = FakeLLM([code_reply("Try.", GENERATED, []) for _ in range(config.MAX_RETRIES)])
    sandbox = FakeSandbox(
        [
            SandboxResult(
                patch_id="x",
                status="fail",
                runtime_seconds=1,
                exit_code=1,
                logs="boom",
                results="crashed",
                delta_firmware="- a\n+ b",
            )
            for _ in range(config.MAX_RETRIES)
        ]
    )
    return Agent(llm, sandbox=sandbox)


# --- the loop asymmetry ------------------------------------------------------


async def test_context_trigger_deploys_a_morph_and_schedules_reversion():
    """LOOPS.md §2: a morph is temporary, so it schedules its own undo."""
    provision()
    orchestrator = Orchestrator(working_agent())
    await orchestrator.handle(make_task())

    with m.SessionLocal() as db:
        row = db.query(m.HistoryRecord).filter_by(record_type=RecordType.MORPH_DEPLOY).one()
        assert row.fw_hash == fw_hash(GENERATED)
        assert row.patch_id is not None  # FKEY back to the plan that produced it
    assert orchestrator.scheduler.pending(DEVICE)


async def test_a_repeat_of_a_live_situation_is_dropped():
    """LOOPS.md §1 + §2a: a device stays over its threshold and re-emits the
    same CONTEXT_TRIGGER every hold period. The second one must not reach the
    model — and must not restart the reversion window, or a situation lasting
    longer than one window would never revert."""
    provision()
    orchestrator = Orchestrator(working_agent())
    await orchestrator.handle(make_task())
    first = orchestrator.scheduler._jobs[DEVICE]

    await orchestrator.handle(make_task())

    with m.SessionLocal() as db:
        assert db.query(m.HistoryRecord).filter_by(record_type=RecordType.MORPH_DEPLOY).count() == 1
    assert orchestrator.scheduler._jobs[DEVICE] is first, "the pending reversion must survive"

    # A *different* situation on the same device is a real new task.
    orchestrator.agent = working_agent()  # one scripted reply per generation
    await orchestrator.handle(make_task(event="LOW_BATTERY"))
    with m.SessionLocal() as db:
        assert db.query(m.HistoryRecord).filter_by(record_type=RecordType.MORPH_DEPLOY).count() == 2


async def test_critical_failure_deploys_a_patch_and_schedules_nothing():
    """LOOPS.md §4.5: an auto-patch is durable — no reversion job."""
    provision()
    orchestrator = Orchestrator(working_agent())
    await orchestrator.handle(make_task(TriggerType.CRITICAL_FAILURE, "UNHANDLED_EXCEPTION"))

    with m.SessionLocal() as db:
        assert db.query(m.HistoryRecord).filter_by(record_type=RecordType.PATCH_DEPLOY).count() == 2
    assert not orchestrator.scheduler.pending(DEVICE)


# --- escalation --------------------------------------------------------------


async def test_exhausted_budget_records_a_strike_and_escalates_the_event():
    """SAFETY_PROTOCOL.md §4: the cap ends in escalation, never another attempt."""
    provision()
    task = make_task()
    await Orchestrator(failing_agent()).handle(task)

    with m.SessionLocal() as db:
        assert db.get(m.Event, task.event_id).escalated is True
        assert db.get(m.Device, DEVICE).strike_count == 1
        # Nothing reached the device.
        assert db.query(m.HistoryRecord).filter_by(record_type=RecordType.MORPH_DEPLOY).count() == 0


async def test_every_attempt_is_persisted_including_rejected_ones():
    """NFR-5: a rejection is evidence the safety layers worked, not noise."""
    provision()
    await Orchestrator(failing_agent()).handle(make_task())
    with m.SessionLocal() as db:
        assert db.query(m.Patch).count() == config.MAX_RETRIES


async def test_third_strike_rolls_back_to_the_last_known_good():
    """PRD Scenario C: the 3rd strike restores the pre-patch artifact."""
    provision()
    deployer.deploy(DEVICE, GENERATED, RecordType.PATCH_DEPLOY)
    deployer.confirm_active(DEVICE, fw_hash(GENERATED))
    rollback.record_strike(DEVICE, "one")
    rollback.record_strike(DEVICE, "two")

    await Orchestrator(failing_agent()).handle(make_task())  # third

    with m.SessionLocal() as db:
        row = db.query(m.HistoryRecord).filter_by(record_type=RecordType.ROLLBACK).one()
        assert row.fw_hash == fw_hash(BASE)
        assert db.get(m.Device, DEVICE).generation_halted is True


async def test_rollback_cancels_a_pending_reversion():
    """A timer still pointing at the morph would later restore firmware that is
    no longer what it replaced."""
    provision()
    orchestrator = Orchestrator(working_agent())
    await orchestrator.handle(make_task())
    assert orchestrator.scheduler.pending(DEVICE)

    rollback.record_strike(DEVICE, "one")
    rollback.record_strike(DEVICE, "two")
    orchestrator.agent = failing_agent()
    # A *new* situation: a repeat of the one already morphed for is dropped
    # before it reaches the model (see the duplicate test above).
    await orchestrator.handle(make_task(event="LOW_BATTERY"))

    assert not orchestrator.scheduler.pending(DEVICE)


# --- the halt gate -----------------------------------------------------------


async def test_a_halted_device_never_reaches_the_model():
    """§5.1: halted means halted until an operator clears it."""
    provision()

    def explode(*args, **kwargs):
        raise AssertionError("a halted device must not reach the Agent")

    agent = working_agent()
    agent.run = explode
    with m.SessionLocal() as db:
        db.get(m.Device, DEVICE).generation_halted = True
        db.commit()

    await Orchestrator(agent).handle(make_task())  # must not raise

    with m.SessionLocal() as db:
        assert db.query(m.HistoryRecord).filter_by(record_type=RecordType.MORPH_DEPLOY).count() == 0


async def test_clearing_strikes_lets_generation_resume():
    """§5.5: explicit operator acknowledgment is the only way back."""
    provision()
    with m.SessionLocal() as db:
        db.get(m.Device, DEVICE).generation_halted = True
        db.commit()
    rollback.clear_strikes(DEVICE)

    await Orchestrator(working_agent()).handle(make_task())
    with m.SessionLocal() as db:
        assert db.query(m.HistoryRecord).filter_by(record_type=RecordType.MORPH_DEPLOY).count() == 1

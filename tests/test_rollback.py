"""M8 check: the 3-strikes protocol, the reversion sub-loop, and the hard
guarantee that neither path can reach an LLM (NFR-4).

Scenario C from PRD §6 lives here: three failures, generation halted, last
known-good restored, no model call anywhere in the sequence.
"""

import asyncio
import socket
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from server.db import models as m  # noqa: E402
from server.deploy import deployer, rollback  # noqa: E402
from server.deploy.scheduler import ReversionScheduler  # noqa: E402
from server.schemas import RecordStatus, RecordType, TriggerType, fw_hash  # noqa: E402

DEVICE = "pi_node_alpha"
BASE = "print('[firmware] baseline', flush=True)\n"
MORPH = "print('[firmware] fan on', flush=True)\n"
PATCH = "print('[firmware] patched', flush=True)\n"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRMWARE_STORE_DIR", tmp_path / "store")
    # Nothing bound: OTA push fails, which the ledger tolerates by design.
    monkeypatch.setattr(config, "OTA_PORT", free_port())
    monkeypatch.setattr(config, "TELEMETRY_TIMEOUT_SECONDS", 1)
    yield  # conftest's clean_db handles table truncation


def provision(active: str = BASE) -> None:
    """A device already running a known-good artifact."""
    deployer.stage_soft_firmware(active)
    with m.SessionLocal() as db:
        db.add(m.Device(id=DEVICE, mcu_type="RaspberryPi_4B", active_fw_hash=fw_hash(active)))
        db.commit()
    deployer.write_history(DEVICE, fw_hash(active), RecordType.PATCH_DEPLOY)


def make_event(trigger=TriggerType.CRITICAL_FAILURE, state_hash=None) -> m.Event:
    with m.SessionLocal() as db:
        event = m.Event(
            device_id=DEVICE,
            trigger_type=trigger,
            event="UNHANDLED_EXCEPTION",
            timestamp=171542000,
            current_state_hash=state_hash or fw_hash(BASE),
            data={"trace": "IndexError"},
        )
        db.add(event)
        db.commit()
        return event


# --- strikes -----------------------------------------------------------------


def test_strikes_accumulate_per_device():
    """§4 vs §5: the retry budget is per event, the strike counter per device."""
    provision()
    assert rollback.record_strike(DEVICE, "sandbox exhausted for event 1") == 1
    assert rollback.record_strike(DEVICE, "sandbox exhausted for event 2") == 2
    assert not rollback.should_roll_back(DEVICE)
    assert rollback.record_strike(DEVICE, "crash after deploy") == 3
    assert rollback.should_roll_back(DEVICE)


def test_strike_limit_comes_from_config(monkeypatch):
    provision()
    monkeypatch.setattr(config, "STRIKE_LIMIT", 2)
    rollback.record_strike(DEVICE, "one")
    assert not rollback.should_roll_back(DEVICE)
    rollback.record_strike(DEVICE, "two")
    assert rollback.should_roll_back(DEVICE)


def test_crash_within_the_window_is_attributable():
    """§5: only a crash tied to a recent deploy counts against it."""
    provision()
    deployer.deploy(DEVICE, PATCH, RecordType.PATCH_DEPLOY)
    event = make_event(state_hash=fw_hash(PATCH))
    assert rollback.crash_is_attributable(DEVICE, event)


def test_stale_crash_is_not_attributable():
    """A device crashing days later reports a new problem, not evidence the
    deployed patch was bad."""
    provision()
    deployer.deploy(DEVICE, PATCH, RecordType.PATCH_DEPLOY)
    with m.SessionLocal() as db:
        row = db.query(m.HistoryRecord).filter_by(status=RecordStatus.DEPLOYED).one()
        row.deployed_at = row.deployed_at - timedelta(
            seconds=config.CRASH_ATTRIBUTION_WINDOW_SECONDS + 60
        )
        db.commit()
    assert not rollback.crash_is_attributable(DEVICE, make_event(state_hash=fw_hash(PATCH)))


def test_crash_from_other_firmware_is_not_attributable():
    """The crash must come from the firmware we actually deployed."""
    provision()
    deployer.deploy(DEVICE, PATCH, RecordType.PATCH_DEPLOY)
    assert not rollback.crash_is_attributable(DEVICE, make_event(state_hash="something_else"))


# --- rollback ----------------------------------------------------------------


def test_rollback_restores_last_known_good_and_halts_generation():
    """PRD Scenario C: the 3rd strike restores the A/B partition's good slot."""
    provision()
    deployer.deploy(DEVICE, PATCH, RecordType.PATCH_DEPLOY)  # the bad one
    deployer.confirm_active(DEVICE, fw_hash(PATCH))

    record_id = rollback.rollback(DEVICE, "3 strikes")

    with m.SessionLocal() as db:
        row = db.get(m.HistoryRecord, record_id)
        assert row.record_type is RecordType.ROLLBACK
        assert row.fw_hash == fw_hash(BASE)  # the pre-patch artifact
        assert row.patch_id is None  # references a prior artifact, not new code
        assert db.get(m.Device, DEVICE).generation_halted is True


def test_halt_survives_a_restart():
    """§5.1: never silently auto-resume — the halt is persisted, not in-memory."""
    provision()
    deployer.deploy(DEVICE, PATCH, RecordType.PATCH_DEPLOY)
    deployer.confirm_active(DEVICE, fw_hash(PATCH))
    rollback.rollback(DEVICE, "3 strikes")
    # A fresh session is what a restarted server sees.
    assert rollback.generation_halted(DEVICE)


def test_generation_halts_even_when_there_is_nothing_to_roll_back_to():
    """§5.1 halts *first* and unconditionally. A device with no known-good
    artifact is exactly the one that must not receive another generated patch,
    so the escalation must not leave generation running."""
    provision()  # only ever ran one firmware; nothing older to restore
    with pytest.raises(rollback.RollbackUnavailable):
        rollback.rollback(DEVICE, "3 strikes")
    assert rollback.generation_halted(DEVICE)


def test_rollback_never_touches_an_llm(monkeypatch):
    """NFR-4: if the model is the thing that's broken, calling it is not safety."""
    import server.agent.agent as agent_module

    def explode(*args, **kwargs):
        raise AssertionError("rollback must not call the LLM")

    monkeypatch.setattr(agent_module, "build_llm", explode)
    monkeypatch.setattr(agent_module.Agent, "run", explode)
    provision()
    deployer.deploy(DEVICE, PATCH, RecordType.PATCH_DEPLOY)
    deployer.confirm_active(DEVICE, fw_hash(PATCH))
    assert rollback.rollback(DEVICE, "3 strikes")


def test_rollback_module_imports_no_llm_machinery():
    """Structural, not behavioural: the import graph itself has no path to the
    Agent, so no future edit can accidentally introduce one silently."""
    source = (ROOT / "server" / "deploy" / "rollback.py").read_text()
    for forbidden in ("langchain", "server.agent", "guardrail", "sandbox_runner"):
        assert forbidden not in source, f"rollback.py must not reference {forbidden}"


def test_rollback_without_a_known_good_artifact_escalates():
    """LOOPS.md §5 failure mode: raise, so it cannot be mistaken for success."""
    with m.SessionLocal() as db:
        db.add(m.Device(id="fresh_device", mcu_type="RaspberryPi_4B"))
        db.commit()
    with pytest.raises(rollback.RollbackUnavailable):
        rollback.rollback("fresh_device", "3 strikes")


def test_clear_strikes_is_operator_only():
    """§5.5: requires explicit acknowledgment; nothing calls this on its own."""
    provision()
    deployer.deploy(DEVICE, PATCH, RecordType.PATCH_DEPLOY)
    deployer.confirm_active(DEVICE, fw_hash(PATCH))
    rollback.rollback(DEVICE, "3 strikes")
    rollback.record_strike(DEVICE, "extra")
    rollback.clear_strikes(DEVICE)
    with m.SessionLocal() as db:
        device = db.get(m.Device, DEVICE)
        assert device.strike_count == 0
        assert device.generation_halted is False


# --- reversion ---------------------------------------------------------------


def test_revert_restores_the_pre_morph_firmware():
    """LOOPS.md §2a: a morph is temporary and must not outlive its situation."""
    provision()
    deployer.deploy(DEVICE, MORPH, RecordType.MORPH_DEPLOY)
    deployer.confirm_active(DEVICE, fw_hash(MORPH))

    record_id = rollback.revert(DEVICE)

    with m.SessionLocal() as db:
        row = db.get(m.HistoryRecord, record_id)
        assert row.record_type is RecordType.REVERSION
        assert row.fw_hash == fw_hash(BASE)


def test_revert_does_not_halt_generation_or_add_a_strike():
    """A reversion is the planned end of a morph, not a failure response."""
    provision()
    deployer.deploy(DEVICE, MORPH, RecordType.MORPH_DEPLOY)
    rollback.revert(DEVICE)
    with m.SessionLocal() as db:
        device = db.get(m.Device, DEVICE)
        assert device.generation_halted is False
        assert device.strike_count == 0


def test_revert_is_a_noop_when_the_live_firmware_is_a_patch():
    """LOOPS.md §4.5: Auto-Patching fixes are durable and never reverted."""
    provision()
    deployer.deploy(DEVICE, PATCH, RecordType.PATCH_DEPLOY)
    assert rollback.revert(DEVICE) is None


# --- scheduler ---------------------------------------------------------------


def reversions() -> int:
    with m.SessionLocal() as db:
        return db.query(m.HistoryRecord).filter_by(record_type=RecordType.REVERSION).count()


async def wait_for_reversion(expected: int = 1, timeout: float = 10.0) -> int:
    """Poll until the reversion lands, instead of sleeping a guessed duration.

    A reversion writes to the DB, and a remote Postgres round-trip is orders of
    magnitude slower than in-memory SQLite — a fixed sleep tuned for one is
    flaky on the other.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if reversions() >= expected:
            break
        await asyncio.sleep(0.02)
    return reversions()


async def test_time_mode_reverts_after_the_window(monkeypatch):
    monkeypatch.setattr(config, "REVERSION_MODE", "time")
    monkeypatch.setattr(config, "REVERSION_WINDOW_SECONDS", 0.1)
    provision()
    deployer.deploy(DEVICE, MORPH, RecordType.MORPH_DEPLOY)

    scheduler = ReversionScheduler()
    scheduler.schedule(DEVICE)

    assert await wait_for_reversion() == 1


async def test_condition_mode_waits_for_recovery(monkeypatch):
    """PRD OQ-1's documented alternative, behind the config flag."""
    monkeypatch.setattr(config, "REVERSION_MODE", "condition")
    monkeypatch.setattr(config, "REVERSION_POLL_SECONDS", 0.05)
    provision()
    deployer.deploy(DEVICE, MORPH, RecordType.MORPH_DEPLOY)

    scheduler = ReversionScheduler()
    scheduler.observe(DEVICE, 85.0)  # still hot
    scheduler.schedule(DEVICE)
    # Negative assertion, so this one must be a real wait: it has to outlast
    # several poll intervals to mean anything.
    await asyncio.sleep(config.REVERSION_POLL_SECONDS * 4)
    assert reversions() == 0

    scheduler.observe(DEVICE, 55.0)  # cooled below REVERSION_RECOVERY_THRESHOLD_C
    assert await wait_for_reversion() == 1


async def test_combined_mode_reverts_on_whichever_comes_first(monkeypatch):
    monkeypatch.setattr(config, "REVERSION_MODE", "combined")
    monkeypatch.setattr(config, "REVERSION_WINDOW_SECONDS", 0.15)
    monkeypatch.setattr(config, "REVERSION_POLL_SECONDS", 0.05)
    provision()
    deployer.deploy(DEVICE, MORPH, RecordType.MORPH_DEPLOY)

    scheduler = ReversionScheduler()
    scheduler.observe(DEVICE, 95.0)  # never recovers; the timer must fire
    scheduler.schedule(DEVICE)

    assert await wait_for_reversion() == 1


async def test_a_newer_morph_replaces_the_pending_reversion(monkeypatch):
    """Two timers racing would revert twice, the second to stale firmware."""
    monkeypatch.setattr(config, "REVERSION_MODE", "time")
    monkeypatch.setattr(config, "REVERSION_WINDOW_SECONDS", 0.15)
    provision()
    deployer.deploy(DEVICE, MORPH, RecordType.MORPH_DEPLOY)

    scheduler = ReversionScheduler()
    scheduler.schedule(DEVICE)
    scheduler.schedule(DEVICE)  # a second morph arrives
    assert scheduler.pending(DEVICE)

    assert await wait_for_reversion() == 1
    # The cancelled timer would fire a window later; give it the chance to, so
    # "exactly one" is a real claim rather than one observed too early.
    await asyncio.sleep(config.REVERSION_WINDOW_SECONDS * 2)
    assert reversions() == 1


async def test_cancelled_reversion_does_not_fire(monkeypatch):
    """Rollback supersedes a pending reversion; the morph is already gone."""
    monkeypatch.setattr(config, "REVERSION_MODE", "time")
    monkeypatch.setattr(config, "REVERSION_WINDOW_SECONDS", 0.1)
    provision()
    deployer.deploy(DEVICE, MORPH, RecordType.MORPH_DEPLOY)

    scheduler = ReversionScheduler()
    scheduler.schedule(DEVICE)
    scheduler.cancel(DEVICE)
    # Negative assertion: outlast the window the cancelled job would have fired
    # in, or this passes simply by looking too soon.
    await asyncio.sleep(config.REVERSION_WINDOW_SECONDS * 3)

    assert reversions() == 0

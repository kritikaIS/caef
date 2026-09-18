"""M10: PRD §6 Scenarios A, B and C, end to end (TDD.md §4.10).

Everything in this file is the real component except the model: real TCP/UDP
Listener, real Distributor, real Guard Rail, real Docker Sandbox, real OTA push
over a socket to a real `edge_node/watchdog.py` subprocess supervising real
firmware, real History Table. Only the LLM is scripted (TDD.md §5 — a live model
in CI is neither deterministic nor free).

The stack runs in-process rather than under docker-compose: the Sandbox already
spawns containers, so composing the server itself would mean nesting Docker for
no extra coverage. `docker-compose.yml` ships for the demo (PRD G6); these tests
drive the same code paths without it.

Scenario timings are compressed via config (sandbox window, reversion window),
never by skipping a stage — every artifact here still passes Guard Rail and the
Sandbox before it reaches the device.
"""

import asyncio
import os
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from frontend import app as frontend  # noqa: E402
from server.agent.agent import Agent  # noqa: E402
from server.agent.rag import indexer  # noqa: E402
from server.api import app as http_app  # noqa: E402
from server.db import models as m  # noqa: E402
from server.deploy import deployer, rollback  # noqa: E402
from server.distributor.distributor import LocalDistributor  # noqa: E402
from server.guardrail import guardrail  # noqa: E402
from server.listener.listener import Listener  # noqa: E402
from server.orchestrator import Orchestrator  # noqa: E402
from server.sandbox import sandbox_runner as sandbox  # noqa: E402
from server.schemas import (  # noqa: E402
    AgentOutput,
    RecordStatus,
    RecordType,
    TelemetryPayload,
    TriggerType,
    fw_hash,
    load_hardware_schema,
)
from tests.fakes import FakeLLM, FakeReply, code_reply, tool_call  # noqa: E402
from tests.test_edge_node import free_port  # noqa: E402

DEVICE = config.DEVICE_ID
BASELINE = (ROOT / "edge_node" / "main.py").read_text()
FAULTY = (ROOT / "tests" / "fixtures" / "firmware_faulty.py").read_text()

pytestmark = pytest.mark.skipif(
    not (sandbox.docker_available() and sandbox.image_exists()),
    reason="E2E needs the Verification Sandbox; without Docker it fails closed by design",
)

# --- the code the scripted model "generates" ---------------------------------

# Scenario A: fan on GPIO_27 enabled, the Lidar driver dropped to free CPU
# (PRD §6 Scenario A step 3).
# It keeps reporting the situation, as a real generated morph does: cooling is
# not instant, so the device stays over threshold and re-fires the trigger for
# as long as the heat lasts (LOOPS.md §1).
MORPH = """import time

import config
from edge_node import telemetry
from edge_node.drivers import DHT11, RelayFan
from server.schemas import TriggerType


def sensor_loop():
    temp_sensor = DHT11(pin=17)
    fan = RelayFan(pin=27)
    fan.on()
    while True:
        temp_c = temp_sensor.read_temp_c()
        print(f"[firmware] cooling temp={temp_c}C fan=ON", flush=True)
        if temp_c > config.HEAT_THRESHOLD_C:
            telemetry.send_event(
                telemetry.build(
                    TriggerType.CONTEXT_TRIGGER,
                    "HIGH_HEAT_DETECTED",
                    {"temp_c": temp_c, "threshold": config.HEAT_THRESHOLD_C},
                )
            )
            time.sleep(config.POST_TRIGGER_HOLD_SECONDS)
            continue
        time.sleep(config.SENSOR_TICK_SECONDS)


sensor_loop()
"""

# Scenario B: the off-by-one in the faulty fixture, corrected.
PATCHED = """import time

import config
from edge_node.drivers import DHT11


def sensor_loop():
    temp_sensor = DHT11(pin=17)
    buffer = [0.0] * 10
    while True:
        buffer.append(temp_sensor.read_temp_c())
        buffer.pop(0)
        for i in range(len(buffer)):
            print(f"[firmware] sample[{i}]={buffer[i]}", flush=True)
        time.sleep(config.SENSOR_TICK_SECONDS)


sensor_loop()
"""

# Scenario C: passes Guard Rail, dies in the Sandbox. Never reaches a device.
BROKEN = """import config
from edge_node.drivers import DHT11

print("[firmware] boot", flush=True)
sensor = DHT11(pin=17)
buffer = [0.0] * 10
for i in range(len(buffer) + 1):
    print(f"[firmware] sample[{i}]={buffer[i]}", flush=True)
"""


def scripted(code: str, pins: list[int], plan: str, rounds: int = 4) -> FakeLLM:
    """A model that checks its pins, then emits `code` — repeatedly, so a second
    trigger arriving mid-test is answered instead of exploding the drain loop."""
    replies = []
    for _ in range(rounds):
        replies.append(FakeReply(tool_calls=[tool_call(pin, f"c{pin}") for pin in pins]))
        replies.append(code_reply(plan, code, pins))
    return FakeLLM(replies)


# --- harness -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def wiring(tmp_path, monkeypatch):
    """Real ports, throwaway store, compressed windows."""
    monkeypatch.setattr(config, "FIRMWARE_STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(config, "RAG_DB_PATH", tmp_path / "rag.db")
    monkeypatch.setattr(config, "LISTENER_TCP_PORT", free_port())
    monkeypatch.setattr(config, "LISTENER_UDP_PORT", free_port())
    monkeypatch.setattr(config, "OTA_PORT", free_port())
    monkeypatch.setattr(config, "DEVICE_OTA_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "SANDBOX_TIMEOUT_SECONDS", 4)
    monkeypatch.setattr(config, "REVERSION_MODE", "time")
    # Longer than POST_TRIGGER_HOLD_SECONDS on the device, so a sustained
    # situation re-fires *while* the morph is still live — the case the
    # duplicate-drop in the Orchestrator exists for.
    monkeypatch.setattr(config, "REVERSION_WINDOW_SECONDS", 12)
    monkeypatch.setattr(config, "TELEMETRY_TIMEOUT_SECONDS", 3)
    frontend.FEED.clear()
    indexer.reindex(DEVICE)
    yield


def provision() -> str:
    """The device as it ships: baseline firmware staged, active, and on the
    ledger — the artifact every rollback in this file must be able to reach."""
    fw = deployer.stage_soft_firmware(BASELINE)
    with m.SessionLocal() as db:
        db.add(
            m.Device(
                id=DEVICE,
                mcu_type="RaspberryPi_4B",
                active_fw_hash=fw,
                assigned_fw_hash=fw,
            )
        )
        db.commit()
    deployer.write_history(DEVICE, fw, RecordType.PATCH_DEPLOY)
    return fw


@asynccontextmanager
async def server(agent: Agent):
    """Listener + Distributor + Orchestrator, exactly as `server/main.py` wires
    them, minus the HTTP server (the dashboard is exercised via TestClient)."""
    distributor = LocalDistributor()
    distributor.subscribe(frontend.record_event)
    orchestrator = Orchestrator(agent)
    listener = Listener(distributor)
    tcp = await listener.serve_tcp()
    udp = await listener.serve_udp()
    drain = asyncio.create_task(distributor.drain(orchestrator.handle))
    try:
        yield orchestrator
    finally:
        drain.cancel()
        orchestrator.scheduler.cancel(DEVICE)
        tcp.close()
        udp.close()
        await asyncio.sleep(0)


class Device:
    """The edge node as a real supervised process: `watchdog.py` running the
    firmware as its child, reachable over the OTA socket."""

    def __init__(self, tmp_path: Path, scenario: str = "normal") -> None:
        self.home = tmp_path / "device"
        self.home.mkdir(exist_ok=True)
        self.firmware = self.home / "main.py"
        self.firmware.write_text(BASELINE)
        self.logfile = self.home / "device.log"
        self.scenario = scenario
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "FIRMWARE_PATH": str(self.firmware),
            "SCENARIO": self.scenario,
            "LISTENER_HOST": "127.0.0.1",
            "LISTENER_TCP_PORT": str(config.LISTENER_TCP_PORT),
            "LISTENER_UDP_PORT": str(config.LISTENER_UDP_PORT),
            "DEVICE_OTA_HOST": "127.0.0.1",
            "OTA_PORT": str(config.OTA_PORT),
            "SENSOR_TICK_SECONDS": "1",
            # Short, as on a real device: heat outlasts one trigger, so the
            # firmware re-fires the same situation while the morph is live
            # (LOOPS.md §1). The server, not the device, is what must not
            # re-generate for it.
            "POST_TRIGGER_HOLD_SECONDS": "5",
            "POLL_INTERVAL_SECONDS": "120",  # push path under test, not the poll path
        }
        self.log = self.logfile.open("w")
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "edge_node" / "watchdog.py")],
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group: kill takes the child too
        )

    def running_hash(self) -> str:
        return fw_hash(self.firmware.read_text())

    def output(self) -> str:
        return self.logfile.read_text()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            self.process.wait(timeout=5)
        self.log.close()


@pytest.fixture
def device(tmp_path):
    node = Device(tmp_path)
    yield node
    node.stop()


async def until(predicate, timeout: float, what: str, device: Device | None = None):
    """Poll a condition rather than sleeping a guessed duration."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.2)
    trailer = f"\ndevice log:\n{device.output()}" if device else ""
    raise AssertionError(f"timed out after {timeout}s waiting for {what}{trailer}")


def history(record_type: RecordType) -> list[m.HistoryRecord]:
    with m.SessionLocal() as db:
        return (
            db.query(m.HistoryRecord)
            .filter_by(record_type=record_type)
            .order_by(m.HistoryRecord.deployed_at)
            .all()
        )


def device_row() -> m.Device:
    with m.SessionLocal() as db:
        return db.get(m.Device, DEVICE)


def events_of(trigger: TriggerType, event: str | None = None) -> list[m.Event]:
    with m.SessionLocal() as db:
        query = db.query(m.Event).filter_by(trigger_type=trigger)
        # Heartbeats are CONTEXT_TRIGGERs too, so counting a *situation* means
        # naming it.
        if event:
            query = query.filter_by(event=event)
        return query.all()


# --- Scenario A: Situational Morphing ----------------------------------------


async def test_scenario_a_heat_event_morphs_then_reverts(tmp_path, device):
    """PRD §6 A: >80°C → CONTEXT_TRIGGER → fan firmware deployed and running on
    the device → reverted to the pre-morph artifact once the window closes."""
    baseline = provision()
    llm = scripted(MORPH, [17, 27], "Enable Relay_Fan on GPIO_27; drop Lidar to free CPU.")
    agent = Agent(llm)
    device.scenario = "heat"

    async with server(agent) as orchestrator:
        device.start()

        await until(
            lambda: events_of(TriggerType.CONTEXT_TRIGGER), 30, "the heat trigger", device
        )
        morph = await until(
            lambda: history(RecordType.MORPH_DEPLOY), 60, "the morph deploy", device
        )

        # The artifact reached the device and the device booted it.
        assert morph[0].fw_hash == fw_hash(MORPH)
        assert morph[0].patch_id, "the deploy must FKEY back to the patch that produced it"
        assert morph[0].event_id, "and to the event that triggered it"
        await until(
            lambda: device.running_hash() == fw_hash(MORPH), 15, "the device to boot the morph", device
        )
        await until(lambda: "fan ON" in device.output(), 15, "the fan to come on", device)
        assert orchestrator.scheduler.pending(DEVICE), "a morph must schedule its own undo"

        # The device is still hot, so it keeps re-firing the same trigger. Each
        # repeat must be dropped: re-generating would burn a model call and,
        # via `schedule`, restart the window the next assertion depends on.
        calls_after_morph = len(llm.invocations)
        await until(
            lambda: len(events_of(TriggerType.CONTEXT_TRIGGER, "HIGH_HEAT_DETECTED")) > 1,
            30,
            "the device to re-fire the same situation",
            device,
        )
        assert len(llm.invocations) == calls_after_morph, "a live situation must not re-generate"
        assert len(history(RecordType.MORPH_DEPLOY)) == 1

        # LOOPS.md §2a: the morph is minimal for a situation and must not
        # outlive it.
        reverted = await until(
            lambda: history(RecordType.REVERSION) and device_row().active_fw_hash == baseline,
            60,
            "the scheduled reversion",
            device,
        )
        assert history(RecordType.REVERSION)[0].fw_hash == baseline
        assert device.running_hash() == baseline
        assert reverted


# --- Scenario B: Auto-Patching -----------------------------------------------


async def test_scenario_b_crash_is_patched_durably(tmp_path, device):
    """PRD §6 B: an IndexError regression arrives by OTA like any other
    firmware; the device crashes, reports the trace, and the generated patch is
    deployed and *stays* — no reversion job."""
    provision()
    agent = Agent(scripted(PATCHED, [17], "Loop bound is off by one; iterate to len(buffer)."))

    async with server(agent) as orchestrator:
        device.start()
        await until(lambda: device.output(), 15, "the device to boot", device)

        # The regression, deployed through the normal path.
        deployer.deploy(DEVICE, FAULTY, RecordType.PATCH_DEPLOY)
        await until(
            lambda: events_of(TriggerType.CRITICAL_FAILURE), 30, "the crash report", device
        )

        crash = events_of(TriggerType.CRITICAL_FAILURE)[0]
        assert "IndexError" in crash.data["trace"]
        assert crash.current_state_hash == fw_hash(FAULTY)

        patched = await until(
            lambda: [r for r in history(RecordType.PATCH_DEPLOY) if r.fw_hash == fw_hash(PATCHED)],
            60,
            "the generated patch to deploy",
            device,
        )
        await until(
            lambda: device.running_hash() == fw_hash(PATCHED), 15, "the device to boot the patch", device
        )

        assert patched[0].event_id == crash.id  # FKEY back to the crash it fixes
        assert not orchestrator.scheduler.pending(DEVICE), "an auto-patch is durable (LOOPS.md §4.5)"
        assert not history(RecordType.REVERSION)
        # SAFETY_PROTOCOL.md §5 / PRD OQ-2: an on-device crash following a deploy
        # counts toward the same strike counter as a sandbox exhaustion.
        assert device_row().strike_count == 1
        assert device_row().generation_halted is False

        # The patch is durable: give the reversion window time to elapse and
        # confirm nothing undoes it.
        await asyncio.sleep(config.REVERSION_WINDOW_SECONDS + 1)
        assert device.running_hash() == fw_hash(PATCHED)


# --- Scenario C: repeated failure → Safety Rollback --------------------------


async def test_scenario_c_third_strike_rolls_back_without_the_llm(tmp_path, device):
    """PRD §6 C: on the 3rd strike the pipeline stops generating and restores
    the last known-good artifact — deterministically, with no further model call.

    Strike 1 is seeded to stand in for an earlier failure on the same event
    chain; strikes 2 (on-device crash) and 3 (retry budget exhausted) are real.
    """
    baseline = provision()
    llm = scripted(BROKEN, [17], "Attempt a fix.")
    agent = Agent(llm)

    async with server(agent):
        device.start()
        await until(lambda: device.output(), 15, "the device to boot", device)
        rollback.record_strike(DEVICE, "prior failure on this event chain")

        deployer.deploy(DEVICE, FAULTY, RecordType.PATCH_DEPLOY)  # crash → strike 2
        await until(
            lambda: events_of(TriggerType.CRITICAL_FAILURE), 30, "the crash report", device
        )

        # Every attempt fails the Sandbox → budget exhausted → strike 3.
        rolled_back = await until(
            lambda: history(RecordType.ROLLBACK), 90, "the safety rollback", device
        )

        assert rolled_back[0].fw_hash == baseline
        assert device_row().generation_halted is True
        assert device_row().strike_count == config.STRIKE_LIMIT
        await until(
            lambda: device.running_hash() == baseline, 15, "the device to boot the rollback", device
        )

        # NFR-4: nothing about the rollback path touched the model, and the
        # broken code never left the Sandbox.
        calls_at_rollback = len(llm.invocations)
        assert not [r for r in history(RecordType.PATCH_DEPLOY) if r.fw_hash == fw_hash(BROKEN)]

        # §5.1: a halted device stays halted — the next trigger is dropped
        # before the Agent is reached, not answered.
        await send_telemetry(TriggerType.CONTEXT_TRIGGER, "HIGH_HEAT_DETECTED", {"temp_c": 91.0})
        await asyncio.sleep(2)
        assert len(llm.invocations) == calls_at_rollback

    # The operator sees it (FR-22): rollback on the ledger, generation halted.
    with TestClient(dashboard_app()) as client:
        page = client.get("/").text
        assert "HALTED" in page
        assert f"{config.STRIKE_LIMIT}/{config.STRIKE_LIMIT}" in page
        assert RecordType.ROLLBACK.value in client.get(f"/device/{DEVICE}").text


async def send_telemetry(trigger: TriggerType, event: str, data: dict) -> None:
    """Raw device telemetry over the real TCP transport."""
    payload = TelemetryPayload(
        id=DEVICE,
        timestamp=171542000,
        trigger_type=trigger,
        event=event,
        data=data,
        current_state_hash=device_row().active_fw_hash or "unknown",
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", config.LISTENER_TCP_PORT)
    writer.write(payload.model_dump_json().encode() + b"\n")
    await writer.drain()
    await reader.readline()
    writer.close()


def dashboard_app():
    if not any(getattr(route, "path", None) == "/" for route in http_app.routes):
        http_app.include_router(frontend.router)
    return http_app


# --- corpus-wide safety invariants (PRD G3, G5) ------------------------------


def test_no_forbidden_pin_reaches_a_device_in_the_test_corpus():
    """PRD G3, stated as a hard requirement rather than a metric: every firmware
    artifact this repo would ever push — baseline, fixtures, and the scenario
    payloads above — is clean under Guard Rail's forbidden-pin check."""
    schema = load_hardware_schema(DEVICE)
    corpus = {
        "baseline": BASELINE,
        "faulty_fixture": FAULTY,
        "scenario_a_morph": MORPH,
        "scenario_b_patch": PATCHED,
        "scenario_c_broken": BROKEN,
    }
    for name, code in corpus.items():
        verdict = guardrail.check(
            AgentOutput(
                patch_id=name,
                event_id="corpus",
                device_id=DEVICE,
                plan="corpus scan",
                target_file="main.py",
                code=code,
                pins_referenced=[],
            ),
            schema,
        )
        assert verdict.checks.forbidden_pin_check == "pass", f"{name}: {verdict.reason}"

"""M3 check: telemetry reaches the Task queue and the Event topic, malformed
payloads are dropped at the gateway, and per-device serialization holds."""

import asyncio
import json
import socket
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from server.agent.stub_agent import StubAgent  # noqa: E402
from server.db import models as m  # noqa: E402
from server.distributor.distributor import LocalDistributor  # noqa: E402
from server.listener.listener import Listener  # noqa: E402
from server.schemas import AgentTask, EventNotification, TriggerType  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _truncate() -> None:
    with m.SessionLocal() as session:
        for table in (m.HistoryRecord, m.Patch, m.Event, m.Device):
            session.query(table).delete()
        session.commit()


@pytest.fixture(autouse=True)
def db():
    # Clean at both ends: a shared in-memory SQLite connection means rows left
    # by another test module would otherwise leak into these assertions.
    m.init_db()
    _truncate()
    yield
    _truncate()


def telemetry(event="HIGH_HEAT_DETECTED", trigger=TriggerType.CONTEXT_TRIGGER, **data) -> bytes:
    return json.dumps(
        {
            "id": config.DEVICE_ID,
            "timestamp": 171542000,
            "trigger_type": trigger,
            "event": event,
            "data": data or {"temp_c": 85.4, "threshold": 80.0},
            "current_state_hash": "a1b2c3d4",
        }
    ).encode()


@pytest.fixture
def wiring():
    distributor = LocalDistributor()
    events: list[EventNotification] = []
    distributor.subscribe(events.append)
    return distributor, Listener(distributor), events


@pytest.mark.asyncio
async def test_context_trigger_becomes_task_and_event(wiring):
    distributor, listener, events = wiring
    agent = StubAgent()
    drain = asyncio.create_task(distributor.drain(agent.handle))

    ack = await listener.handle(telemetry())
    assert ack["status"] == "queued"
    await distributor.join()
    drain.cancel()

    assert len(agent.seen) == 1
    task = agent.seen[0]
    assert task.trigger_type is TriggerType.CONTEXT_TRIGGER
    assert task.event == "HIGH_HEAT_DETECTED"
    assert task.event_id == ack["event_id"]
    # Same occurrence went to the observability leg too (ARCHITECTURE.md §3).
    assert [e.event for e in events] == ["HIGH_HEAT_DETECTED"]

    with m.SessionLocal() as db_session:
        stored = db_session.get(m.Event, ack["event_id"])
        assert stored.timestamp == 171542000  # device-authoritative, not server
        assert stored.data["temp_c"] == 85.4
        # First contact auto-registers the device so the Event has a valid FKEY.
        assert db_session.get(m.Device, config.DEVICE_ID).active_fw_hash == "a1b2c3d4"


@pytest.mark.asyncio
async def test_critical_failure_carries_trace_to_agent(wiring):
    distributor, listener, _ = wiring
    agent = StubAgent()
    drain = asyncio.create_task(distributor.drain(agent.handle))

    await listener.handle(
        telemetry(
            event="UNHANDLED_EXCEPTION",
            trigger=TriggerType.CRITICAL_FAILURE,
            trace="IndexError: list index out of range",
        )
    )
    await distributor.join()
    drain.cancel()

    assert agent.seen[0].trigger_type is TriggerType.CRITICAL_FAILURE
    assert "IndexError" in agent.seen[0].raw_payload["data"]["trace"]


@pytest.mark.asyncio
async def test_heartbeat_is_observed_but_does_not_wake_the_agent(wiring):
    distributor, listener, events = wiring
    agent = StubAgent()
    drain = asyncio.create_task(distributor.drain(agent.handle))

    ack = await listener.handle(telemetry(event="HEARTBEAT", temp_c=45.0))
    await distributor.join()
    drain.cancel()

    assert ack["status"] == "ack"
    assert agent.seen == []  # routine liveness is not work for the Agent
    assert [e.event for e in events] == ["HEARTBEAT"]


@pytest.mark.asyncio
async def test_malformed_payload_dropped_at_the_gateway(wiring):
    distributor, listener, events = wiring
    agent = StubAgent()
    drain = asyncio.create_task(distributor.drain(agent.handle))

    assert (await listener.handle(b"not json at all"))["status"] == "rejected"
    # Valid JSON, but not a Telemetry Payload.
    assert (await listener.handle(b'{"id": "x"}'))["status"] == "rejected"
    # Unknown trigger_type must not reach the Agent as an unhandled enum.
    assert (await listener.handle(telemetry(trigger="WHATEVER")))["status"] == "rejected"

    await distributor.join()
    drain.cancel()
    assert agent.seen == []
    assert events == []
    with m.SessionLocal() as db_session:
        assert db_session.query(m.Event).count() == 0


@pytest.mark.asyncio
async def test_tasks_serialize_per_device(wiring):
    """TDD.md §2.3: one in-flight generation per device, so two patches never
    race for the same target file — but distinct devices still overlap."""
    distributor, _, _ = wiring
    concurrent: dict[str, int] = {}
    peak: dict[str, int] = {}
    overlapped = asyncio.Event()

    async def slow_handler(task: AgentTask) -> None:
        concurrent[task.device_id] = concurrent.get(task.device_id, 0) + 1
        peak[task.device_id] = max(peak.get(task.device_id, 0), concurrent[task.device_id])
        if len(concurrent) > 1:
            overlapped.set()
        await asyncio.sleep(0.05)
        concurrent[task.device_id] -= 1

    drain = asyncio.create_task(distributor.drain(slow_handler))
    for device in ("dev_a", "dev_a", "dev_a", "dev_b", "dev_b"):
        await distributor.publish_task(
            AgentTask(
                task_id=str(uuid.uuid4()),
                event_id=str(uuid.uuid4()),
                device_id=device,
                trigger_type=TriggerType.CONTEXT_TRIGGER,
                event="HIGH_HEAT_DETECTED",
                raw_payload={},
            )
        )
    await distributor.join()
    drain.cancel()

    assert peak == {"dev_a": 1, "dev_b": 1}
    assert overlapped.is_set(), "distinct devices must not block each other"


@pytest.mark.asyncio
async def test_handler_failure_does_not_kill_the_drain_loop(wiring):
    distributor, _, _ = wiring
    handled: list[str] = []

    async def flaky(task: AgentTask) -> None:
        if task.event == "BOOM":
            raise RuntimeError("agent exploded")
        handled.append(task.event)

    drain = asyncio.create_task(distributor.drain(flaky))
    for event in ("BOOM", "HIGH_HEAT_DETECTED"):
        await distributor.publish_task(
            AgentTask(
                task_id=str(uuid.uuid4()),
                event_id=str(uuid.uuid4()),
                device_id="dev_a",
                trigger_type=TriggerType.CONTEXT_TRIGGER,
                event=event,
                raw_payload={},
            )
        )
    await distributor.join()
    assert not drain.done()
    drain.cancel()
    assert handled == ["HIGH_HEAT_DETECTED"]


@pytest.mark.asyncio
async def test_broken_subscriber_does_not_block_telemetry(wiring):
    """Observability is off the safety path (NFR-6)."""
    distributor, listener, events = wiring
    distributor.subscribe(lambda _: (_ for _ in ()).throw(RuntimeError("frontend down")))
    agent = StubAgent()
    drain = asyncio.create_task(distributor.drain(agent.handle))

    assert (await listener.handle(telemetry()))["status"] == "queued"
    await distributor.join()
    drain.cancel()
    assert len(agent.seen) == 1


@pytest.mark.asyncio
async def test_device_to_listener_over_real_sockets(wiring, monkeypatch):
    """The M2 telemetry client talking to the real M3 gateway."""
    from edge_node import telemetry as client

    distributor, listener, events = wiring
    monkeypatch.setattr(config, "LISTENER_TCP_PORT", free_port())
    monkeypatch.setattr(config, "LISTENER_UDP_PORT", free_port())
    agent = StubAgent()

    server = await listener.serve_tcp()
    transport = await listener.serve_udp()
    drain = asyncio.create_task(distributor.drain(agent.handle))
    try:
        payload = client.build(
            TriggerType.CONTEXT_TRIGGER, "HIGH_HEAT_DETECTED", {"temp_c": 85.4}
        )
        ack = await asyncio.to_thread(client.send_event, payload)
        assert ack["status"] == "queued"

        await asyncio.to_thread(
            client.send_heartbeat,
            client.build(TriggerType.CONTEXT_TRIGGER, "HEARTBEAT", {"temp_c": 45.0}),
        )
        await asyncio.sleep(0.2)  # UDP is fire-and-forget; no ack to await
        await distributor.join()
    finally:
        drain.cancel()
        transport.close()
        server.close()
        await server.wait_closed()

    assert [t.event for t in agent.seen] == ["HIGH_HEAT_DETECTED"]
    assert {e.event for e in events} == {"HIGH_HEAT_DETECTED", "HEARTBEAT"}

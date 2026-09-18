"""Listener — Telemetry Gateway (TDD.md §2.2).

Terminates the device connection, validates the inbound payload against the
Telemetry Payload schema, persists the `Event`, then hands work to the
Distributor as a `Task` (to the Agent) and an `Event` (to observers).

UDP for heartbeats, TCP for triggers expecting an ack. Malformed payloads are
logged and dropped, never forwarded — the Agent must not be asked to reason
about a payload the gateway could not parse.
"""

import asyncio
import json
import logging
import uuid

import config
from server.db.models import Device, Event, SessionLocal
from server.distributor.distributor import Distributor
from server.schemas import AgentTask, EventNotification, TelemetryPayload, TriggerType

log = logging.getLogger("caef.listener")

# Routine liveness beat; carries no situation to reason about, so it updates the
# device record and stops there rather than waking the Agent.
HEARTBEAT_EVENT = "HEARTBEAT"


def record_event(payload: TelemetryPayload) -> Event:
    """Persist the Event and upsert the device's last-reported state.

    `received_at` is defaulted server-side purely for latency metrics; `time`
    ordering stays device-authoritative (ARCHITECTURE.md §4.2).
    """
    with SessionLocal() as db:
        device = db.get(Device, payload.id)
        if device is None:
            # First contact from an unprovisioned device: register it so its
            # events have somewhere to FKEY to. mcu_type comes from the hardware
            # schema at provisioning; unknown until then.
            device = Device(id=payload.id, mcu_type="unknown")
            db.add(device)
        if device.active_fw_hash is None:
            device.active_fw_hash = payload.current_state_hash

        event = Event(
            device_id=payload.id,
            trigger_type=payload.trigger_type,
            event=payload.event,
            timestamp=payload.timestamp,
            current_state_hash=payload.current_state_hash,
            data=payload.data,
        )
        db.add(event)
        db.commit()
        return event


class Listener:
    def __init__(self, distributor: Distributor) -> None:
        self.distributor = distributor
        # Strong refs to in-flight UDP handlers; asyncio only holds weak ones,
        # so an unreferenced task can be garbage-collected mid-await.
        self._udp_inflight: set[asyncio.Task] = set()

    async def handle(self, raw: bytes) -> dict:
        """Validate, persist, dispatch. Returns the ack sent back over TCP."""
        try:
            payload = TelemetryPayload.model_validate_json(raw)
        except ValueError as exc:
            log.warning("dropped malformed payload: %s", exc)
            return {"status": "rejected", "reason": "malformed_payload"}

        event = record_event(payload)
        log.info(
            "%s %s from %s (hash=%s)",
            payload.trigger_type,
            payload.event,
            payload.id,
            payload.current_state_hash,
        )

        await self.distributor.publish_event(
            EventNotification(
                event_id=event.id,
                device_id=payload.id,
                trigger_type=payload.trigger_type,
                event=payload.event,
                timestamp=payload.timestamp,
                current_state_hash=payload.current_state_hash,
                data=payload.data,
            )
        )

        if payload.event == HEARTBEAT_EVENT:
            return {"status": "ack", "event_id": event.id}

        await self.distributor.publish_task(
            AgentTask(
                task_id=str(uuid.uuid4()),
                event_id=event.id,
                device_id=payload.id,
                trigger_type=payload.trigger_type,
                event=payload.event,
                raw_payload=payload.model_dump(),
            )
        )
        return {"status": "queued", "event_id": event.id}

    # --- transports ----------------------------------------------------------

    async def serve_tcp(self) -> asyncio.AbstractServer:
        async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                raw = await reader.readline()
                if raw.strip():
                    ack = await self.handle(raw)
                    writer.write(json.dumps(ack).encode() + b"\n")
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError) as exc:
                log.warning("device connection dropped: %s", exc)
            finally:
                writer.close()

        server = await asyncio.start_server(
            on_connect, config.LISTENER_HOST, config.LISTENER_TCP_PORT
        )
        log.info("TCP listening on %s:%s", config.LISTENER_HOST, config.LISTENER_TCP_PORT)
        return server

    async def serve_udp(self) -> asyncio.DatagramTransport:
        listener = self

        class Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr) -> None:
                # Fire-and-forget: no ack channel, so nothing to await on.
                task = asyncio.create_task(listener.handle(data))
                listener._udp_inflight.add(task)
                task.add_done_callback(listener._udp_inflight.discard)

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Protocol, local_addr=(config.LISTENER_HOST, config.LISTENER_UDP_PORT)
        )
        log.info("UDP listening on %s:%s", config.LISTENER_HOST, config.LISTENER_UDP_PORT)
        return transport

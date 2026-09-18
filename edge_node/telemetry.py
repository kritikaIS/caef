"""Telemetry client (LOOPS.md §1 body, TRD.md §2.1).

Lives outside main.py on purpose: main.py is the artifact the Agent regenerates
wholesale (FR-14), so the transport must not be inside the file being replaced.

UDP for routine heartbeats (best-effort, low latency). TCP for CONTEXT_TRIGGER
and CRITICAL_FAILURE, which expect an eventual ack/reply.
"""

import json
import socket
import time
from pathlib import Path

import config
from server.schemas import TelemetryPayload, TriggerType, fw_hash


def current_state_hash() -> str:
    """Hash of the firmware this device is running right now (DATA_SCHEMAS.md §2).
    Feeds the Poll/Reconciliation Loop's drift detection."""
    path = Path(config.FIRMWARE_PATH)
    return fw_hash(path.read_text()) if path.exists() else "unknown"


def build(trigger_type: TriggerType, event: str, data: dict) -> TelemetryPayload:
    return TelemetryPayload(
        id=config.DEVICE_ID,
        # The device is the clock (ARCHITECTURE.md §4.2) — server-side receipt
        # time is recorded separately and only used for latency metrics.
        timestamp=int(time.time()),
        trigger_type=trigger_type,
        event=event,
        data=data,
        current_state_hash=current_state_hash(),
    )


def send_heartbeat(payload: TelemetryPayload) -> None:
    """Fire-and-forget UDP. A dropped heartbeat is not an error condition."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(
            payload.model_dump_json().encode(),
            (config.LISTENER_HOST, config.LISTENER_UDP_PORT),
        )


def send_event(payload: TelemetryPayload) -> dict | None:
    """TCP full-duplex send for events that expect a reply. Returns the server's
    ack, or None if the server is unreachable — an unreachable server must leave
    the device running its current firmware, never crash it (NFR-6)."""
    try:
        with socket.create_connection(
            (config.LISTENER_HOST, config.LISTENER_TCP_PORT),
            timeout=config.TELEMETRY_TIMEOUT_SECONDS,
        ) as sock:
            sock.sendall(payload.model_dump_json().encode() + b"\n")
            sock.shutdown(socket.SHUT_WR)
            reply = sock.makefile("r").readline()
            return json.loads(reply) if reply.strip() else None
    except OSError as exc:
        print(f"[telemetry] listener unreachable: {exc}", flush=True)
        return None

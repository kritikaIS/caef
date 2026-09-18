"""M2 check: sensor loop emits the right triggers, watchdog verifies OTA hashes
and restarts, crashed firmware holds idle instead of busy-crash-looping."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from server.schemas import (  # noqa: E402
    OTAAck,
    OTAPush,
    RecordType,
    TelemetryPayload,
    TriggerType,
    fw_hash,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeListener:
    """Collects device telemetry over UDP + TCP, acks TCP events."""

    def __init__(self) -> None:
        self.udp_port = free_port()
        self.tcp_port = free_port()
        self.received: list[TelemetryPayload] = []
        self.stop = threading.Event()

    def start(self) -> None:
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", self.udp_port))
        self.udp.settimeout(0.5)
        self.tcp = socket.socket()
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", self.tcp_port))
        self.tcp.listen(4)
        self.tcp.settimeout(0.5)
        threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._tcp_loop, daemon=True).start()

    def _udp_loop(self) -> None:
        while not self.stop.is_set():
            try:
                data, _ = self.udp.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            self.received.append(TelemetryPayload.model_validate_json(data))

    def _tcp_loop(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.tcp.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                line = conn.makefile("r").readline()
                if line.strip():
                    self.received.append(TelemetryPayload.model_validate_json(line))
                conn.sendall(b'{"status":"queued"}\n')

    def shutdown(self) -> None:
        self.stop.set()
        self.udp.close()
        self.tcp.close()

    def of_type(self, trigger: TriggerType) -> list[TelemetryPayload]:
        return [p for p in self.received if p.trigger_type is trigger]


@pytest.fixture
def listener():
    lst = FakeListener()
    lst.start()
    yield lst
    lst.shutdown()


def run_firmware(path: Path, listener: FakeListener, seconds: float, scenario="normal", **extra):
    """Run a firmware file as a real subprocess, as the watchdog would."""
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "LISTENER_UDP_PORT": str(listener.udp_port),
        "LISTENER_TCP_PORT": str(listener.tcp_port),
        "SCENARIO": scenario,
        "SENSOR_TICK_SECONDS": "1",
        "FIRMWARE_PATH": str(path),
        **extra,
    }
    proc = subprocess.Popen(
        [sys.executable, str(path)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=5)
    return proc, proc.stdout.read().decode()


def test_heartbeats_under_normal_conditions(listener):
    _, out = run_firmware(ROOT / "edge_node" / "main.py", listener, 3.0)
    beats = [p for p in listener.of_type(TriggerType.CONTEXT_TRIGGER) if p.event == "HEARTBEAT"]
    assert beats, f"no heartbeats received. output:\n{out}"
    assert beats[0].id == config.DEVICE_ID
    assert beats[0].current_state_hash != "unknown"  # device knows its own firmware
    assert not [p for p in listener.received if p.event == "HIGH_HEAT_DETECTED"]


def test_heat_event_emits_context_trigger(listener):
    _, out = run_firmware(ROOT / "edge_node" / "main.py", listener, 6.0, scenario="heat")
    heat = [p for p in listener.received if p.event == "HIGH_HEAT_DETECTED"]
    assert heat, f"no CONTEXT_TRIGGER emitted. output:\n{out}"
    ev = heat[0]
    assert ev.trigger_type is TriggerType.CONTEXT_TRIGGER
    assert ev.data["temp_c"] > ev.data["threshold"]
    assert ev.timestamp > 0  # device-authoritative timestamp


def test_crash_emits_critical_failure_with_trace_then_exits(listener):
    proc, out = run_firmware(ROOT / "tests" / "fixtures" / "firmware_faulty.py", listener, 15.0)
    crashes = listener.of_type(TriggerType.CRITICAL_FAILURE)
    assert crashes, f"no CRITICAL_FAILURE emitted. output:\n{out}"
    assert "IndexError" in crashes[0].data["trace"]
    # Exits rather than looping, so the watchdog holds it idle awaiting a patch.
    assert proc.returncode == 1


def test_watchdog_rejects_hash_mismatch(tmp_path, monkeypatch):
    """Integrity check per TRD §6: a tampered payload must not be written."""
    from edge_node.watchdog import Watchdog

    fw = tmp_path / "main.py"
    fw.write_text("print('good')\n")
    monkeypatch.setattr(config, "FIRMWARE_PATH", fw)

    wd = Watchdog()
    ack = wd.apply_ota(
        OTAPush(
            device_id=config.DEVICE_ID,
            fw_hash="0000000000000000",  # wrong on purpose
            target_file="main.py",
            code="print('evil')\n",
            record_type=RecordType.PATCH_DEPLOY,
        ).model_dump_json()
    )
    assert ack.status == "rejected"
    assert "hash_mismatch" in ack.reason
    assert fw.read_text() == "print('good')\n"  # device kept its firmware


def test_watchdog_applies_valid_ota_and_restarts(tmp_path, monkeypatch):
    from edge_node.watchdog import Watchdog

    fw = tmp_path / "main.py"
    fw.write_text("import time\nprint('v1')\ntime.sleep(30)\n")
    monkeypatch.setattr(config, "FIRMWARE_PATH", fw)

    wd = Watchdog()
    wd.start_firmware()
    first_pid = wd.child.pid
    new_code = "import time\nprint('v2')\ntime.sleep(30)\n"
    try:
        ack = wd.apply_ota(
            OTAPush(
                device_id=config.DEVICE_ID,
                fw_hash=fw_hash(new_code),
                target_file="main.py",
                code=new_code,
                record_type=RecordType.MORPH_DEPLOY,
            ).model_dump_json()
        )
        assert ack.status == "accepted"
        assert fw.read_text() == new_code
        assert wd.running_hash() == fw_hash(new_code)
        assert wd.child.pid != first_pid  # restarted into the new firmware
    finally:
        if wd.child and wd.child.poll() is None:
            wd.child.kill()


def test_watchdog_ota_socket_roundtrip(tmp_path, monkeypatch):
    """End-to-end over the real socket the deployer will push to."""
    from edge_node.watchdog import Watchdog

    fw = tmp_path / "main.py"
    code = "print('deployed')\n"
    fw.write_text("print('old')\n")
    port = free_port()
    monkeypatch.setattr(config, "FIRMWARE_PATH", fw)
    monkeypatch.setattr(config, "OTA_PORT", port)

    wd = Watchdog()
    threading.Thread(target=wd.serve_ota, daemon=True).start()
    time.sleep(0.3)
    try:
        push = OTAPush(
            device_id=config.DEVICE_ID,
            fw_hash=fw_hash(code),
            target_file="main.py",
            code=code,
            record_type=RecordType.ROLLBACK,
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(push.model_dump_json().encode() + b"\n")
            ack = OTAAck.model_validate_json(sock.makefile("r").readline())
        assert ack.status == "accepted"
        assert fw.read_text() == code
    finally:
        wd.stop.set()
        if wd.child and wd.child.poll() is None:
            wd.child.kill()


def test_watchdog_survives_malformed_push(tmp_path, monkeypatch):
    from edge_node.watchdog import Watchdog

    fw = tmp_path / "main.py"
    fw.write_text("print('good')\n")
    port = free_port()
    monkeypatch.setattr(config, "FIRMWARE_PATH", fw)
    monkeypatch.setattr(config, "OTA_PORT", port)

    wd = Watchdog()
    threading.Thread(target=wd.serve_ota, daemon=True).start()
    time.sleep(0.3)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"not json at all\n")
            ack = json.loads(sock.makefile("r").readline())
        assert ack["status"] == "rejected"
        assert fw.read_text() == "print('good')\n"
        # Still serving after a bad payload.
        good = "print('ok')\n"
        push = OTAPush(
            device_id=config.DEVICE_ID,
            fw_hash=fw_hash(good),
            target_file="main.py",
            code=good,
            record_type=RecordType.PATCH_DEPLOY,
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(push.model_dump_json().encode() + b"\n")
            ack2 = OTAAck.model_validate_json(sock.makefile("r").readline())
        assert ack2.status == "accepted"
    finally:
        wd.stop.set()
        if wd.child and wd.child.poll() is None:
            wd.child.kill()


def test_telemetry_survives_unreachable_listener(monkeypatch):
    """NFR-6: no server means the device keeps running, not crashes."""
    from edge_node import telemetry

    monkeypatch.setattr(config, "LISTENER_TCP_PORT", free_port())  # nothing bound
    monkeypatch.setattr(config, "TELEMETRY_TIMEOUT_SECONDS", 1)
    payload = telemetry.build(TriggerType.CRITICAL_FAILURE, "UNHANDLED_EXCEPTION", {"trace": "x"})
    assert telemetry.send_event(payload) is None

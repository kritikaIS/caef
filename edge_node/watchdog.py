"""Device supervisor: OTA intake, safe restart, poll reconciliation.

Owns two loops:
  - OTA listener (TDD.md §2.1): accepts an OTA Push Payload, verifies fw_hash,
    writes the artifact, restarts the firmware child process.
  - Poll / Reconciliation Loop (LOOPS.md §3): asks the server what firmware this
    device is *supposed* to run, so a dropped OTA push never strands the device
    on stale firmware.

The watchdog is the parent process and the firmware runs as its child. That is
what makes the restart safe (TDD.md §2.1: "never a raw exit() that could strand
the device without a supervisor") — if the firmware dies, something is still
alive to receive the patch that fixes it.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config
from server.schemas import OTAAck, OTAPush, PollResponse, fw_hash


def log(msg: str) -> None:
    print(f"[watchdog] {msg}", flush=True)


class Watchdog:
    def __init__(self) -> None:
        self.firmware_path = Path(config.FIRMWARE_PATH)
        self.child: subprocess.Popen | None = None
        self.stop = threading.Event()

    # --- firmware child ------------------------------------------------------

    def start_firmware(self) -> None:
        # subprocess is on the generated-code denylist (config.DENYLIST_CALLS is
        # about Agent output); the supervisor itself is hand-written and must
        # spawn its child somehow.
        #
        # The child needs the repo importable: firmware imports `config` and
        # `edge_node.drivers`, and a generated artifact carries no sys.path
        # shim of its own — nor should it, since FIRMWARE_PATH can point
        # anywhere. Giving it the environment is the supervisor's job.
        env = {**os.environ, "PYTHONPATH": str(config.ROOT)}
        self.child = subprocess.Popen([sys.executable, str(self.firmware_path)], env=env)
        log(f"firmware started pid={self.child.pid} hash={self.running_hash()}")

    def restart_firmware(self) -> None:
        """Controlled restart into new firmware."""
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
        self.start_firmware()

    def running_hash(self) -> str:
        return fw_hash(self.firmware_path.read_text()) if self.firmware_path.exists() else "unknown"

    # --- OTA intake ----------------------------------------------------------

    def apply_ota(self, raw: str) -> OTAAck:
        """Verify then write. Integrity check per TRD.md §6 — a mismatched hash
        is rejected and the device keeps running its current firmware."""
        push = OTAPush.model_validate_json(raw)
        actual = fw_hash(push.code)
        if actual != push.fw_hash:
            log(f"REJECTED push: hash_mismatch (claimed {push.fw_hash}, got {actual})")
            return OTAAck(
                device_id=config.DEVICE_ID,
                status="rejected",
                fw_hash=push.fw_hash,
                reason=f"hash_mismatch: claimed {push.fw_hash}, computed {actual}",
            )

        # Write to a temp file and replace atomically, so a crash mid-write can
        # never leave the device with a half-written firmware file.
        tmp = self.firmware_path.with_suffix(".py.incoming")
        tmp.write_text(push.code)
        tmp.replace(self.firmware_path)
        log(f"applied {push.record_type} hash={push.fw_hash}; restarting")
        self.restart_firmware()
        return OTAAck(device_id=config.DEVICE_ID, status="accepted", fw_hash=push.fw_hash)

    def serve_ota(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Bound from the device's own side of DEVICE_OTA_HOST: loopback on a
            # single-machine run, 0.0.0.0 when the edge node is its own container
            # and the server reaches it by hostname.
            srv.bind((config.DEVICE_OTA_HOST, config.OTA_PORT))
            srv.listen(4)
            srv.settimeout(1.0)
            log(f"OTA listening on {config.DEVICE_OTA_HOST}:{config.OTA_PORT}")
            while not self.stop.is_set():
                try:
                    conn, _ = srv.accept()
                except TimeoutError:
                    continue
                with conn:
                    raw = conn.makefile("r").readline()
                    if not raw.strip():
                        continue
                    try:
                        ack = self.apply_ota(raw)
                    except Exception as exc:  # malformed push must not kill the device
                        log(f"malformed push: {exc}")
                        ack = OTAAck(
                            device_id=config.DEVICE_ID,
                            status="rejected",
                            fw_hash="",
                            reason=f"malformed_payload: {exc}",
                        )
                    conn.sendall(ack.model_dump_json().encode() + b"\n")

    # --- poll / reconciliation ----------------------------------------------

    def poll_once(self) -> PollResponse | None:
        query = urllib.parse.urlencode(
            {"id": config.DEVICE_ID, "current_state_hash": self.running_hash()}
        )
        url = f"http://{config.API_HOST}:{config.API_PORT}/poll?{query}"
        try:
            with urllib.request.urlopen(url, timeout=config.TELEMETRY_TIMEOUT_SECONDS) as resp:
                return PollResponse.model_validate_json(resp.read())
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # Connectivity failure is an infra concern, explicitly outside the
            # Safety Rollback Protocol's scope (LOOPS.md §3 failure mode): log,
            # do not attempt local auto-remediation.
            log(f"poll failed: {exc}")
            return None

    def fetch_assigned_firmware(self) -> None:
        """Missed-push recovery: re-request the assigned artifact directly."""
        url = f"http://{config.API_HOST}:{config.API_PORT}/firmware?id={config.DEVICE_ID}"
        try:
            with urllib.request.urlopen(url, timeout=config.TELEMETRY_TIMEOUT_SECONDS) as resp:
                self.apply_ota(resp.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            log(f"firmware re-request failed: {exc}")

    def poll_loop(self) -> None:
        while not self.stop.is_set():
            result = self.poll_once()
            if result and not result.in_sync:
                log(f"drift: running {self.running_hash()}, assigned {result.assigned_fw_hash}")
                self.fetch_assigned_firmware()
            self.stop.wait(config.POLL_INTERVAL_SECONDS)

    # --- supervision ---------------------------------------------------------

    def run(self) -> None:
        self.start_firmware()
        for target in (self.serve_ota, self.poll_loop):
            threading.Thread(target=target, daemon=True).start()
        try:
            while True:
                # A crashed firmware holds in a safe idle state awaiting a patch
                # or rollback — it must not busy-crash-loop (LOOPS.md §1).
                if self.child and self.child.poll() is not None:
                    log(f"firmware exited rc={self.child.returncode}; holding for patch")
                    self.child = None
                time.sleep(config.SENSOR_TICK_SECONDS)
        except KeyboardInterrupt:
            self.stop.set()
            if self.child and self.child.poll() is None:
                self.child.terminate()
            log("shutdown")


if __name__ == "__main__":
    Watchdog().run()

"""Verification Sandbox — time-boxed, isolated execution (SAFETY_PROTOCOL.md §3).

Runs a Guard-Rail-passed candidate inside a container that mirrors the device
runtime, under CPU/memory/pid caps and a hard wall clock, and returns the
literal `FAIL (Results, ΔFirmware)` artifact from ARCHITECTURE.md §3 on failure.

Two rules shape everything here:

- **Fails closed.** If Docker is missing or the image is unbuilt, the verdict is
  `fail`, never `pass` and never "skip the sandbox". A broken verifier must not
  become a path to production (SAFETY_PROTOCOL.md §1 layer 4: "no exceptions").
- **Timeout is a pass signal, not a failure.** Firmware is an infinite sensor
  loop; surviving the whole window without crashing is exactly the healthy
  outcome (§3.4). A candidate that exits early with a non-zero code failed.
"""

import difflib
import logging
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import config
from server.schemas import AgentOutput, SandboxResult

log = logging.getLogger("caef.sandbox")

# Exit code Docker reports when the container is killed by the runner's timeout.
_SIGKILL_EXIT = 137
# Where candidates are staged, relative to the repo root. Inside the repo so the
# single read-only repo mount reaches them; see `run`.
SANDBOX_WORKDIR = ".sandbox"
_DOCKER_TIMEOUT_MARGIN_SECONDS = 5


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def image_exists(image: str = None) -> bool:
    image = image or config.SANDBOX_IMAGE
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return result.returncode == 0


def build_image(image: str = None) -> None:
    """Build the sandbox image. Called by tests and by the compose bootstrap."""
    image = image or config.SANDBOX_IMAGE
    dockerfile = Path(__file__).parent / "Dockerfile.sandbox"
    subprocess.run(
        # --network=host for the *build* only: the default bridge has no DNS on
        # some hosts, so pip can't resolve pypi. The sandbox container itself
        # still runs with --network none (SAFETY_PROTOCOL.md §3.3).
        ["docker", "build", "--network=host", "-f", str(dockerfile), "-t", image,
         str(dockerfile.parent)],
        check=True,
        capture_output=True,
    )


def delta_firmware(candidate: str, last_known_good: str | None) -> str | None:
    """ΔFirmware: unified diff of attempted vs last-known-good, so the Agent's
    next attempt sees what it changed, not just that it broke."""
    if last_known_good is None:
        return None
    diff = difflib.unified_diff(
        last_known_good.splitlines(keepends=True),
        candidate.splitlines(keepends=True),
        fromfile="last_known_good",
        tofile="candidate",
    )
    return "".join(diff) or None


def _failed(patch_id: str, reason: str, logs: str = "", runtime: float = 0.0) -> SandboxResult:
    return SandboxResult(
        patch_id=patch_id,
        status="fail",
        runtime_seconds=runtime,
        exit_code=-1,
        logs=logs,
        results=reason,
    )


def run(output: AgentOutput, last_known_good: str | None = None) -> SandboxResult:
    """Execute the candidate. Never raises: a verifier that throws is a verifier
    that stops verifying, so every failure mode resolves to a `fail` verdict."""
    if not docker_available():
        return _failed(
            output.patch_id,
            "sandbox_unavailable: docker is not usable; failing closed rather than "
            "letting unverified firmware through (SAFETY_PROTOCOL.md §1 layer 4)",
        )
    if not image_exists():
        return _failed(
            output.patch_id,
            f"sandbox_unavailable: image {config.SANDBOX_IMAGE} not built",
        )

    container = f"caef-sandbox-{uuid.uuid4().hex[:12]}"
    # Staged inside the repo rather than /tmp so the single repo mount carries it
    # too: under docker-compose the runner is itself a container talking to the
    # host daemon, and a second `-v` of a runner-local temp path would bind
    # something that does not exist on the host.
    workroot = Path(config.ROOT) / SANDBOX_WORKDIR
    workroot.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workroot) as workdir:
        # The container runs as a non-root uid that does not exist on the host,
        # so both the directory and the file must be world-readable or the
        # interpreter cannot open the candidate.
        Path(workdir).chmod(0o755)
        candidate = Path(workdir) / "candidate.py"
        candidate.write_text(output.code)
        candidate.chmod(0o644)

        repo = Path(config.SANDBOX_HOST_REPO).resolve()
        mounted = f"/caef/{SANDBOX_WORKDIR}/{Path(workdir).name}/candidate.py"
        command = [
            "docker", "run", "--rm", "--name", container,
            # Untrusted code: no network, no privilege escalation, no writable
            # root, capped CPU/memory/pids (SAFETY_PROTOCOL.md §3.3).
            "--network", "none",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=16m",
            "--cap-drop", "ALL",
            "--pids-limit", "64",
            "--memory", config.SANDBOX_MEMORY_LIMIT,
            "--cpus", config.SANDBOX_CPU_LIMIT,
            # Repo read-only so a candidate cannot rewrite the drivers, the
            # hardware schema, or the pipeline it is being tested by — including
            # the staged candidate itself, which lives under this same mount.
            "-v", f"{repo}:/caef:ro",
            "-e", "PYTHONPATH=/caef",
            "-e", "PYTHONUNBUFFERED=1",
            # The candidate must not reach a real listener from inside the
            # sandbox; --network none already guarantees this, and the firmware's
            # own unreachable-listener path (NFR-6) handles it gracefully.
            "-e", f"FIRMWARE_PATH={mounted}",
            "-e", f"SCENARIO={config.SCENARIO}",
            config.SANDBOX_IMAGE,
            "python", mounted,
        ]

        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=config.SANDBOX_TIMEOUT_SECONDS,
            )
            exit_code, logs = completed.returncode, completed.stdout + completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = _SIGKILL_EXIT
            logs = (exc.stdout or b"").decode(errors="replace") + (
                exc.stderr or b""
            ).decode(errors="replace")
            subprocess.run(
                ["docker", "kill", container],
                capture_output=True,
                timeout=_DOCKER_TIMEOUT_MARGIN_SECONDS,
            )
        except OSError as exc:
            return _failed(output.patch_id, f"sandbox_error: {exc}")

        runtime = round(time.monotonic() - started, 3)

    # An infinite sensor loop is *supposed* to still be running when the window
    # closes; that is the healthy verdict (§3.4). Exiting early is only healthy
    # if it exited cleanly.
    healthy = timed_out or exit_code == 0
    marker_present = not config.SANDBOX_HEALTHY_MARKER or config.SANDBOX_HEALTHY_MARKER in logs

    if healthy and marker_present:
        return SandboxResult(
            patch_id=output.patch_id,
            status="pass",
            runtime_seconds=runtime,
            exit_code=0 if not timed_out else exit_code,
            logs=logs[-config.SANDBOX_LOG_LIMIT_CHARS :],
        )

    if not healthy:
        results = f"Process exited with code {exit_code} after {runtime}s"
    else:
        results = (
            f"Ran {runtime}s without emitting the healthy marker "
            f"{config.SANDBOX_HEALTHY_MARKER!r}"
        )
    log.info("sandbox FAIL patch=%s: %s", output.patch_id, results)
    return SandboxResult(
        patch_id=output.patch_id,
        status="fail",
        runtime_seconds=runtime,
        exit_code=exit_code,
        logs=logs[-config.SANDBOX_LOG_LIMIT_CHARS :],
        results=results,
        delta_firmware=delta_firmware(output.code, last_known_good),
    )

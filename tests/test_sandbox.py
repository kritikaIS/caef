"""M5 check: the Sandbox verdict is correct for hand-written good/bad scripts,
the container is actually confined, and the runner fails closed.

No LLM here either (TDD.md §5). Docker-dependent cases are skipped when Docker
is unavailable; the fail-closed cases run everywhere, because "no Docker" must
never silently mean "no verification".
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from server.sandbox import sandbox_runner as sandbox  # noqa: E402
from server.schemas import AgentOutput  # noqa: E402

HEALTHY = """
import time

while True:
    print('[firmware] temp=45.0C', flush=True)
    time.sleep(0.5)
"""

CRASHES = """
print('[firmware] up', flush=True)
buffer = [0.0] * 10
for i in range(len(buffer) + 1):
    print(f'[firmware] sample[{i}]={buffer[i]}', flush=True)
"""


def candidate(code: str, patch_id: str = "p1") -> AgentOutput:
    return AgentOutput(
        patch_id=patch_id,
        event_id="e1",
        device_id="pi_node_alpha",
        plan="test candidate",
        target_file="main.py",
        code=code,
    )


def docker_ready() -> bool:
    return sandbox.docker_available() and sandbox.image_exists()


needs_docker = pytest.mark.skipif(not docker_ready(), reason="docker/sandbox image unavailable")


@pytest.fixture(autouse=True)
def short_window(monkeypatch):
    """Keep the suite fast; the semantics under test don't depend on 10s."""
    monkeypatch.setattr(config, "SANDBOX_TIMEOUT_SECONDS", 4)


# --- fail-closed (no Docker required) ----------------------------------------


def test_missing_docker_fails_closed(monkeypatch):
    """SAFETY_PROTOCOL.md §1 layer 4: a broken verifier must not become a path
    to production. No Docker means fail, never pass and never skip."""
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)
    result = sandbox.run(candidate(HEALTHY))
    assert result.status == "fail"
    assert "sandbox_unavailable" in result.results


def test_missing_image_fails_closed(monkeypatch):
    monkeypatch.setattr(sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(sandbox, "image_exists", lambda *a: False)
    result = sandbox.run(candidate(HEALTHY))
    assert result.status == "fail"
    assert "not built" in result.results


def test_delta_firmware_is_a_real_diff():
    """FAIL(Results, ΔFirmware) — the Agent's next attempt needs the delta."""
    delta = sandbox.delta_firmware("print('new')\n", "print('old')\n")
    assert "-print('old')" in delta and "+print('new')" in delta
    # Nothing to diff against on a device's first patch.
    assert sandbox.delta_firmware("print('new')\n", None) is None


# --- verdict correctness (Docker) --------------------------------------------


@needs_docker
def test_healthy_loop_passes():
    """§3.4: an infinite sensor loop is *supposed* to outlive the window."""
    result = sandbox.run(candidate(HEALTHY))
    assert result.status == "pass", result.results
    assert "[firmware]" in result.logs
    assert result.runtime_seconds >= config.SANDBOX_TIMEOUT_SECONDS
    assert result.delta_firmware is None


@needs_docker
def test_crashing_candidate_fails_with_trace_and_delta():
    """PRD Scenario B's bug, caught before it ever reaches a device."""
    result = sandbox.run(candidate(CRASHES), last_known_good=HEALTHY)
    assert result.status == "fail"
    assert result.exit_code == 1
    assert "IndexError" in result.logs
    assert "exited with code 1" in result.results
    assert result.delta_firmware  # never silently discarded (§3.5)


@needs_docker
def test_clean_exit_passes():
    """Not every candidate loops forever; exiting 0 with the marker is healthy."""
    result = sandbox.run(candidate("print('[firmware] one-shot ok', flush=True)\n"))
    assert result.status == "pass", result.results
    assert result.exit_code == 0


@needs_docker
def test_silent_candidate_fails_the_healthy_marker_check():
    """§3.4: surviving the window is necessary but not sufficient — the
    candidate must show it is actually doing its job."""
    result = sandbox.run(candidate("import time\ntime.sleep(60)\n"))
    assert result.status == "fail"
    assert "healthy marker" in result.results


@needs_docker
def test_hang_is_bounded_by_the_configured_timeout(monkeypatch):
    """§3.2: time-boxed, and the box comes from config, not a literal."""
    monkeypatch.setattr(config, "SANDBOX_TIMEOUT_SECONDS", 2)
    result = sandbox.run(candidate("print('[firmware] up', flush=True)\nwhile True:\n    pass\n"))
    assert result.runtime_seconds < 10  # would run forever unbounded
    assert result.status == "pass"  # survived its window; a busy loop is Guard Rail's job


@needs_docker
def test_immediate_syntax_error_fails_fast():
    result = sandbox.run(candidate("def broken(:\n"))
    assert result.status == "fail"
    assert "SyntaxError" in result.logs


# --- confinement (Docker) ----------------------------------------------------


@needs_docker
def test_candidate_has_no_network():
    """§3.3 / --network none: exfiltration and phone-home are off the table."""
    code = (
        "import socket\n"
        "print('[firmware] up', flush=True)\n"
        "socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
    )
    result = sandbox.run(candidate(code))
    assert result.status == "fail"
    assert "Network is unreachable" in result.logs or "OSError" in result.logs


@needs_docker
def test_candidate_cannot_write_to_the_repo():
    """The candidate must not rewrite the hardware schema, the drivers, or the
    pipeline that is testing it (SAFETY_PROTOCOL.md §1 layer 1)."""
    code = (
        "print('[firmware] up', flush=True)\n"
        "open('/caef/schemas/schema_dev01.json', 'w').write('{}')\n"
    )
    result = sandbox.run(candidate(code))
    assert result.status == "fail"
    assert "Read-only file system" in result.logs or "Permission denied" in result.logs
    # And the real file on the host is untouched.
    assert "pi_node_alpha" in (ROOT / "schemas" / "schema_dev01.json").read_text()


@needs_docker
def test_candidate_runs_as_non_root():
    result = sandbox.run(candidate("import os\nprint(f'[firmware] uid={os.getuid()}', flush=True)\n"))
    assert result.status == "pass", result.results
    assert "uid=0" not in result.logs


@needs_docker
def test_memory_cap_is_enforced():
    """§3.3: caps are set at container level, not left to defaults."""
    code = "print('[firmware] up', flush=True)\nx = bytearray(512 * 1024 * 1024)\n"
    result = sandbox.run(candidate(code))
    assert result.status == "fail"


@needs_docker
def test_no_containers_are_left_running():
    """Every run is --rm; a leaked container is a leaked resource cap."""
    sandbox.run(candidate(HEALTHY))
    listed = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=caef-sandbox-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    assert listed.stdout.strip() == ""


@needs_docker
def test_baseline_firmware_passes_the_sandbox():
    """Regression floor: what we ship must survive our own verifier."""
    result = sandbox.run(candidate((ROOT / "edge_node" / "main.py").read_text()))
    assert result.status == "pass", result.results


@needs_docker
def test_faulty_fixture_fails_the_sandbox():
    """The Scenario B fixture must be caught here, not on the device."""
    code = (ROOT / "tests" / "fixtures" / "firmware_faulty.py").read_text()
    result = sandbox.run(candidate(code), last_known_good=HEALTHY)
    assert result.status == "fail"
    assert "IndexError" in result.logs

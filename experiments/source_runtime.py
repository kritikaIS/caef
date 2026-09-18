"""Executes a model-authored source candidate against the virtual world.

RESEARCH.md §12. The baseline arms have to actually *run* their firmware, or the
comparison is between a measured system and an unmeasured one. So the candidate
runs in a child interpreter and exchanges JSON lines with this process: one
observation in, one list of actuator commands out.

**This is a subprocess and a timeout, not a sandbox.** It is a process boundary
and a wall clock — enough to stop a `while True` candidate from hanging the
harness and to keep a crash from taking the harness with it, and nothing more.
The child is started with `-I` (no user site, no `PYTHONPATH`) in a scratch
directory, which limits accidents rather than intent.

That is acceptable here because in the default configuration the candidates come
from `StubSourceAgent` — this repository's own fixtures, not a model. With
`--llm` the harness refuses to execute model-authored source unless the operator
passes `--i-understand-execute-source`, because "we ran whatever the model wrote,
outside a sandbox" is not a thing to do by default.
"""

import json
import select
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import config

RUNNER = '''"""Child-side harness. Loads the candidate and answers one observation at a time."""
import json
import sys
from pathlib import Path

# Firmware logs. The protocol keeps its own handle on the real stdout and hands
# the candidate stderr instead, so a `print()` in the candidate cannot corrupt
# the channel — which is not a hypothetical: the baseline firmware prints on
# every tick by design.
protocol = sys.stdout
sys.stdout = sys.stderr

path = Path(sys.argv[1])
namespace = {"__name__": "candidate", "__file__": str(path)}
try:
    exec(compile(path.read_text(), str(path), "exec"), namespace)
except BaseException as exc:  # a candidate that will not even load
    print(json.dumps({"fatal": f"{type(exc).__name__}: {exc}"}), file=protocol, flush=True)
    raise SystemExit(1)

step = namespace.get("step")
if not callable(step):
    print(json.dumps({"fatal": "candidate defines no callable step()"}), file=protocol, flush=True)
    raise SystemExit(1)

print(json.dumps({"ready": True}), file=protocol, flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        intents = step(json.loads(line))
        if intents is None:
            intents = []
        print(json.dumps({"intents": list(intents)}), file=protocol, flush=True)
    except BaseException as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=protocol, flush=True)
'''


class SourceFault(RuntimeError):
    """The candidate died, hung, or answered with something unusable."""


@dataclass
class StepOutcome:
    intents: list[dict]
    error: str | None = None


class SourceController:
    """A model-authored `step()` running in a child interpreter."""

    def __init__(self, code: str, step_timeout_seconds: float = 2.0) -> None:
        self.code = code
        self.step_timeout = step_timeout_seconds
        self._directory: tempfile.TemporaryDirectory | None = None
        self._process: subprocess.Popen | None = None
        self.faulted = False
        self.fault_reason: str | None = None

    def __enter__(self) -> "SourceController":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def start(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="caef-source-")
        root = Path(self._directory.name)
        candidate = root / "candidate.py"
        candidate.write_text(self.code)
        runner = root / "_runner.py"
        runner.write_text(RUNNER)

        self._process = subprocess.Popen(
            # -I: isolated. No user site-packages, no PYTHONPATH, no cwd on the
            # import path beyond the scratch directory itself.
            [sys.executable, "-I", str(runner), str(candidate)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=root,
        )
        handshake = self._read_line(timeout=self.step_timeout * 3)
        if handshake is None or "ready" not in handshake:
            self._fault(
                (handshake or {}).get("fatal", "candidate did not start")
                if isinstance(handshake, dict)
                else "candidate did not start"
            )

    def step(self, observation: dict) -> StepOutcome:
        """One control step. A hang, a crash or a bad answer is a fault."""
        if self.faulted or self._process is None or self._process.poll() is not None:
            raise SourceFault(self.fault_reason or "candidate is not running")

        try:
            self._process.stdin.write(json.dumps(observation) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            self._fault(f"candidate closed its input: {exc}")

        reply = self._read_line(timeout=self.step_timeout)
        if reply is None:
            self._fault(f"candidate did not answer within {self.step_timeout}s")
        if "fatal" in reply:
            self._fault(reply["fatal"])
        if "error" in reply:
            # The candidate raised inside step(). It is still alive, so this is
            # one bad step rather than a dead controller — the arm decides what
            # to make of it.
            return StepOutcome(intents=[], error=reply["error"])
        intents = reply.get("intents", [])
        if not isinstance(intents, list):
            self._fault("candidate returned a non-list from step()")
        return StepOutcome(intents=[item for item in intents if isinstance(item, dict)])

    def close(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=5)
        self._process = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None

    # --- internals -----------------------------------------------------------

    def _read_line(self, timeout: float) -> dict | None:
        """Read one JSON line, or None if the child does not produce one in time."""
        assert self._process is not None
        ready, _, _ = select.select([self._process.stdout], [], [], timeout)
        if not ready:
            return None
        line = self._process.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"fatal": f"candidate wrote non-JSON: {line[:120]!r}"}

    def _fault(self, reason: str) -> None:
        self.faulted = True
        self.fault_reason = reason
        self.close()
        raise SourceFault(reason)


def step_timeout_seconds() -> float:
    """Per-step budget, derived from the configured whole-run cap."""
    return max(1.0, config.EXPERIMENT_SOURCE_TIMEOUT_SECONDS / 10.0)

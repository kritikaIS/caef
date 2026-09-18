"""Persistent device state: A/B slots, the active lease, the sequence watermark.

RESEARCH.md §8/§10. The v0.1 device kept none of this — A/B slots lived only in
the server's database and a temporary morph's reversion was an `asyncio.Task` in
the server process. Both facts mean the same thing: if the server went away, the
device could not undo anything.

Here the device owns it. Everything needed to expire a lease, revert to
last-known-good and refuse a replayed package is on the device's own disk, so a
restart of the supervisor, of the server, or of both, changes nothing about what
the device will do next.

**Atomic writes.** State is written to a temporary file in the same directory,
flushed, `fsync`'d and then `os.replace`'d over the target. A crash mid-write
leaves the previous state intact; it can never leave a half-written file that
would strand the device with no idea which slot is good.

**On clocks.** A lease has to survive a restart, and a monotonic clock does not.
The lease therefore carries both a simulated/monotonic elapsed count and the
wall-clock time it was installed, and expiry uses whichever has advanced *more*.
A stopped clock cannot extend a lease and a rewound one cannot either. A device
whose real-time clock an attacker controls would need a secure monotonic counter;
that is out of scope here and stated as a limitation (RESEARCH.md §14).
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from server.compiler.program import ControllerProgram

log = logging.getLogger("caef.slots")

STATE_VERSION = 1
SLOT_A = "A"
SLOT_B = "B"
SLOTS = (SLOT_A, SLOT_B)

CandidateStatus = Literal["none", "probation", "active", "failed"]


class Lease(BaseModel):
    """A bounded permission to run, expiring locally."""

    model_config = ConfigDict(extra="forbid")

    duration_seconds: int = Field(gt=0)
    elapsed_seconds: float = 0.0
    installed_at_wall: float

    def advance(self, delta_seconds: float) -> None:
        self.elapsed_seconds += delta_seconds

    def consumed(self, now_wall: float) -> float:
        """Whichever clock has advanced further since installation."""
        return max(self.elapsed_seconds, max(0.0, now_wall - self.installed_at_wall))

    def remaining(self, now_wall: float) -> float:
        return max(0.0, self.duration_seconds - self.consumed(now_wall))

    def expired(self, now_wall: float) -> bool:
        return self.consumed(now_wall) >= self.duration_seconds


class SlotRecord(BaseModel):
    """One firmware partition. `artifact` is data, so a slot is literally a
    document — there is no image to flash and nothing to execute."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    artifact: ControllerProgram | None = None
    artifact_hash: str | None = None
    manifest_id: str | None = None
    sequence_number: int | None = None
    installed_at_wall: float | None = None
    lease: Lease | None = None

    @property
    def empty(self) -> bool:
        return self.artifact is None


class DeviceState(BaseModel):
    """Everything the device needs to act alone."""

    model_config = ConfigDict(extra="forbid")

    state_version: int = STATE_VERSION
    device_id: str
    slots: dict[str, SlotRecord]
    active_slot: str = SLOT_A
    last_known_good_slot: str = SLOT_A
    candidate_slot: str | None = None
    candidate_status: CandidateStatus = "none"
    probation_ticks_remaining: int = 0
    failure_count: int = 0
    # The replay watermark. Persisted, so a restart cannot be used to re-play a
    # package the device already accepted (RESEARCH.md §9).
    last_accepted_sequence: int = 0

    @property
    def running_slot(self) -> str:
        """What is executing right now.

        During probation that is the candidate, while `active_slot` still names
        the artifact the device would fall back to. Conflating the two is how a
        device ends up with no known-good left.
        """
        if self.candidate_status == "probation" and self.candidate_slot:
            return self.candidate_slot
        return self.active_slot

    @property
    def inactive_slot(self) -> str:
        return SLOT_B if self.active_slot == SLOT_A else SLOT_A

    def slot(self, name: str) -> SlotRecord:
        return self.slots[name]

    def running(self) -> SlotRecord:
        return self.slots[self.running_slot]

    def last_known_good(self) -> SlotRecord:
        return self.slots[self.last_known_good_slot]

    def active_lease(self) -> Lease | None:
        return self.running().lease


def blank_state(device_id: str) -> DeviceState:
    return DeviceState(
        device_id=device_id,
        slots={name: SlotRecord(slot=name) for name in SLOTS},
    )


# --- persistence -------------------------------------------------------------


def save_state(state: DeviceState, path: Path) -> None:
    """Atomic write: temp file in the same directory, fsync, then replace.

    Same directory because `os.replace` is only atomic within a filesystem, and
    `fsync` before the replace because a rename that lands before the data does
    is exactly the corruption this is meant to prevent.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True)

    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_state(path: Path, device_id: str) -> DeviceState:
    """Read persisted state, or start fresh.

    A state file that cannot be parsed is treated as absent and logged. The
    alternative — refusing to boot — would leave a device with no supervisor at
    all, which is strictly worse than one that has forgotten its slots.
    """
    path = Path(path)
    if not path.exists():
        return blank_state(device_id)
    try:
        state = DeviceState.model_validate_json(path.read_text())
    except ValueError:
        log.exception("device state at %s is unreadable; starting from blank state", path)
        return blank_state(device_id)

    if state.device_id != device_id:
        log.warning(
            "device state at %s belongs to %s, not %s; starting from blank state",
            path,
            state.device_id,
            device_id,
        )
        return blank_state(device_id)
    if state.state_version != STATE_VERSION:
        log.warning(
            "device state at %s is version %s, this build writes %s; starting from blank state",
            path,
            state.state_version,
            STATE_VERSION,
        )
        return blank_state(device_id)
    return state

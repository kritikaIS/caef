"""The virtual device: supervisor, A/B slots, leases, probation, local rollback.

RESEARCH.md §7/§8/§10. This is the half of the system that has to keep working
when the other half is gone. Everything it does — expiring a lease, failing a
candidate out of probation, reverting to last-known-good, refusing a replayed
package — is decided from its own persisted state, with no server, no network
and no model in the path.

The tick order is fixed and mirrors `server/sim/harness.py`, with the device's
own bookkeeping wrapped around it:

    1. the world advances
    2. sensors are read
    3. the supervisor's local policy runs (emergency cooling, safe state)
    4. the lease is charged, and expires locally if it is spent
    5. the controller is stepped, if it still has control
    6. the supervisor considers its intents
    7. probation is scored: a healthy tick advances it, a fault ends it

Step 4 before step 5 matters: an expired morph does not get one more control
step, and it does not need the server to tell it so.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import config
from edge_node import slots as slot_store
from edge_node.slots import DeviceState, Lease, SlotRecord
from edge_node.supervisor import SafetyState, SafetySupervisor, SupervisorPolicy
from server.compiler.program import ActuatorIntent, ControllerProgram, IntentDecision
from server.compiler.runtime import (
    CompiledController,
    ControllerContext,
    ControllerFault,
    Observation,
)
from server.manifest.registry import CapabilityRegistry
from server.ota.package import FirmwarePackage, PackageVerdict, verify_package
from server.schemas import HardwareSchema
from server.sim.world import ThermalWorld

log = logging.getLogger("caef.device")


@dataclass
class InstallOutcome:
    """What happened to one package offered to this device."""

    accepted: bool
    verdict: PackageVerdict
    slot: str | None = None
    lease_seconds: int | None = None

    @property
    def reason(self) -> str:
        return self.verdict.reason


@dataclass
class TickReport:
    tick: int
    time_s: float
    device_temp_c: float
    sensor_temp_c: float
    fan_state: str
    supervisor_state: str
    emergency_active: bool
    running_slot: str
    running_manifest: str | None
    lease_remaining_s: float | None
    intents: list[ActuatorIntent] = field(default_factory=list)
    decisions: list[IntentDecision] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


class VirtualDevice:
    """A simulated edge node that owns its own firmware lifecycle."""

    def __init__(
        self,
        device_id: str,
        world: ThermalWorld,
        schema: HardwareSchema,
        registry: CapabilityRegistry,
        state_path: Path,
        policy: SupervisorPolicy | None = None,
        telemetry_sink: Callable[[ActuatorIntent], None] | None = None,
        wall_clock: Callable[[], float] = time.time,
        signing_key: bytes | None = None,
    ) -> None:
        self.device_id = device_id
        self.world = world
        self.schema = schema
        self.registry = registry
        self.state_path = Path(state_path)
        self.wall_clock = wall_clock
        self.signing_key = signing_key
        self.telemetry_sink = telemetry_sink

        self.state: DeviceState = slot_store.load_state(self.state_path, device_id)
        self.supervisor = SafetySupervisor(
            port=world,
            schema=schema,
            registry=registry,
            policy=policy,
            telemetry_sink=telemetry_sink,
        )
        self.controller: CompiledController | None = None
        self.events: list[tuple[int, str]] = []
        self._boot_running_slot()

    # --- boot ----------------------------------------------------------------

    def _boot_running_slot(self) -> None:
        """Start whatever the persisted state says should be running.

        A restart resumes the same slot with the same lease, because both came
        off disk. That is the whole point of persisting them (RESEARCH.md §8).
        """
        record = self.state.running()
        if record.artifact is None:
            return
        self._start(record.artifact)
        remaining = (
            record.lease.remaining(self.wall_clock()) if record.lease else None
        )
        log.info(
            "booted slot %s manifest=%s lease_remaining=%s",
            self.state.running_slot,
            record.manifest_id,
            "none" if remaining is None else f"{remaining:.1f}s",
        )

    def _start(self, program: ControllerProgram) -> None:
        self.supervisor.install_program(program)
        self.supervisor.leave_safe_state()
        self.controller = CompiledController(program)
        self.controller.initialize(
            ControllerContext(
                device_id=self.device_id,
                started_at_s=self.world.time_s,
                actuator_states=dict(self.world.actuators),
            )
        )

    def provision(self, program: ControllerProgram) -> None:
        """Install a factory image into slot A with no package and no lease.

        This is the artifact the device ships with; it is the floor every
        rollback lands on, so it is never given a lease.
        """
        self.state.slots[slot_store.SLOT_A] = SlotRecord(
            slot=slot_store.SLOT_A,
            artifact=program,
            artifact_hash=program.artifact_hash,
            manifest_id=program.manifest_id,
            sequence_number=0,
            installed_at_wall=self.wall_clock(),
            lease=None,
        )
        self.state.active_slot = slot_store.SLOT_A
        self.state.last_known_good_slot = slot_store.SLOT_A
        self.state.candidate_slot = None
        self.state.candidate_status = "none"
        self.state.failure_count = 0
        self._start(program)
        self._save()
        self._record("provisioned", f"slot A manifest={program.manifest_id}")

    # --- installation --------------------------------------------------------

    @property
    def running_artifact_hash(self) -> str | None:
        return self.state.running().artifact_hash

    def install(self, package: FirmwarePackage) -> InstallOutcome:
        """Verify a package locally, then stage it into the inactive slot.

        Verification is entirely local: signature, device, artifact integrity,
        base-firmware freshness, replay watermark, lease ceiling and registry
        version. A device that could only decide this by asking the server would
        have no answer during exactly the outage this design is about.
        """
        verdict = verify_package(
            package,
            device_id=self.device_id,
            current_firmware_hash=self.running_artifact_hash,
            last_accepted_sequence=self.state.last_accepted_sequence,
            key=self.signing_key,
        )
        if not verdict.accepted:
            self._record("package_rejected", f"{verdict.rejection}: {verdict.reason}")
            return InstallOutcome(accepted=False, verdict=verdict)

        target = self.state.inactive_slot
        lease = (
            Lease(
                duration_seconds=package.lease_duration_seconds,
                installed_at_wall=self.wall_clock(),
            )
            if package.lease_duration_seconds is not None
            else None
        )
        self.state.slots[target] = SlotRecord(
            slot=target,
            artifact=package.artifact,
            artifact_hash=package.artifact_hash,
            manifest_id=package.artifact.manifest_id,
            sequence_number=package.sequence_number,
            installed_at_wall=self.wall_clock(),
            lease=lease,
        )
        # The watermark moves on acceptance, not on activation: a package that
        # was accepted and then failed probation must not be replayable.
        self.state.last_accepted_sequence = package.sequence_number
        self.state.candidate_slot = target
        self.state.candidate_status = "probation"
        self.state.probation_ticks_remaining = config.PROBATION_HEALTHY_TICKS

        self._start(package.artifact)
        self._save()
        self._record(
            "installed_candidate",
            f"slot {target} manifest={package.artifact.manifest_id} seq="
            f"{package.sequence_number} lease="
            f"{package.lease_duration_seconds or 'none'}s probation="
            f"{config.PROBATION_HEALTHY_TICKS} ticks",
        )
        return InstallOutcome(
            accepted=True,
            verdict=verdict,
            slot=target,
            lease_seconds=package.lease_duration_seconds,
        )

    # --- the tick ------------------------------------------------------------

    def tick(self) -> TickReport:
        snapshot = self.world.step()
        metrics = self.world.metrics()
        events: list[str] = []

        self.supervisor.enforce_local_policy(metrics, snapshot.tick, snapshot.time_s)

        events.extend(self._charge_lease(snapshot.tick))

        intents: list[ActuatorIntent] = []
        faulted = False
        if self.controller is not None and self.supervisor.state is SafetyState.NORMAL:
            try:
                intents = self.controller.step(
                    Observation(
                        tick=snapshot.tick,
                        time_s=snapshot.time_s,
                        metrics=metrics,
                        actuator_states=dict(self.world.actuators),
                    )
                )
            except ControllerFault as exc:
                faulted = True
                events.append("controller_fault")
                self._record("controller_fault", str(exc))
                self.supervisor.on_controller_fault(exc)
                # Safe state takes effect this tick, not the next one.
                self.supervisor.enforce_local_policy(metrics, snapshot.tick, snapshot.time_s)

        decisions = self.supervisor.apply(intents, metrics, snapshot.time_s)

        if faulted:
            events.extend(self._handle_fault(snapshot.tick))
        else:
            events.extend(self._score_probation(snapshot.tick))

        after = self.world.snapshot()
        record = self.state.running()
        return TickReport(
            tick=snapshot.tick,
            time_s=snapshot.time_s,
            device_temp_c=snapshot.device_temp_c,
            sensor_temp_c=snapshot.sensor_temp_c,
            fan_state=after.fan_state,
            supervisor_state=self.supervisor.state.value,
            emergency_active=self.supervisor.emergency_active,
            running_slot=self.state.running_slot,
            running_manifest=record.manifest_id,
            lease_remaining_s=(
                record.lease.remaining(self.wall_clock()) if record.lease else None
            ),
            intents=intents,
            decisions=decisions,
            events=events,
        )

    def _charge_lease(self, tick: int) -> list[str]:
        """Advance the running lease and revert if it is spent.

        Charged from simulated time, checked against wall time too, and acted on
        with no reference to the server (RESEARCH.md §8). Returns the events for
        this tick — `reverted` included, so a caller reading the tick report
        sees the same facts the device's own log records.
        """
        record = self.state.running()
        lease = record.lease
        if lease is None:
            return []
        lease.advance(self.world.config.tick_seconds)
        if not lease.expired(self.wall_clock()):
            # Persist the charge so a restart resumes where this left off.
            self._save()
            return []
        self._record("lease_expired", f"slot {record.slot} manifest={record.manifest_id}")
        reverted = self.revert_local(
            f"lease expired after {lease.duration_seconds}s", tick=tick
        )
        return ["lease_expired", "reverted"] if reverted else ["lease_expired"]

    def _handle_fault(self, tick: int) -> list[str]:
        """A candidate that faults fails probation immediately; an established
        artifact gets the local crash budget before it is rolled back."""
        self.state.failure_count += 1
        if self.state.candidate_status == "probation":
            self.state.candidate_status = "failed"
            self._save()
            self.revert_local("candidate faulted during probation", tick=tick)
            return ["probation_failed", "reverted"]
        if self.state.failure_count >= config.LOCAL_FAILURE_LIMIT:
            self._save()
            self.revert_local(
                f"{self.state.failure_count} local failures", tick=tick
            )
            return ["failure_limit_reached", "reverted"]
        self._save()
        return []

    def _score_probation(self, tick: int) -> list[str]:
        if self.state.candidate_status != "probation":
            return []
        if self.supervisor.state is not SafetyState.NORMAL:
            self.state.candidate_status = "failed"
            self._save()
            self.revert_local("candidate left the supervisor in safe state", tick=tick)
            return ["probation_failed", "reverted"]

        self.state.probation_ticks_remaining -= 1
        if self.state.probation_ticks_remaining > 0:
            self._save()
            return []

        # Promotion. The outgoing slot becomes last-known-good and is not
        # overwritten, so there is always a real artifact to fall back to
        # (SAFETY_PROTOCOL.md §6).
        previous = self.state.active_slot
        self.state.last_known_good_slot = previous
        self.state.active_slot = self.state.candidate_slot or previous
        self.state.candidate_status = "active"
        self.state.failure_count = 0
        self._save()
        self._record(
            "candidate_activated",
            f"slot {self.state.active_slot} promoted; slot {previous} is last-known-good",
        )
        return ["candidate_activated"]

    # --- local recovery ------------------------------------------------------

    def revert_local(self, reason: str, tick: int | None = None) -> bool:
        """Restore last-known-good. No server, no model, no network.

        Runs the outgoing controller's declared fallback first, so a morph that
        promised to leave cooling on gets to keep that promise on its way out.
        """
        target = self.state.last_known_good()
        if target.artifact is None:
            # Nothing to fall back to. The supervisor's safe state is then the
            # whole of the device's safety, which is why it keeps cooling rather
            # than merely stopping the firmware.
            self.supervisor.enter_safe_state(f"{reason}; no known-good artifact")
            self._record("revert_unavailable", reason)
            return False

        if self.controller is not None:
            fallback = self.controller.shutdown(reason)
            self.supervisor.apply(fallback, self.world.metrics(), self.world.time_s)

        outgoing = self.state.running_slot
        self.state.slots[outgoing].lease = None
        if self.state.candidate_status == "probation":
            self.state.candidate_status = "failed"
        self.state.active_slot = self.state.last_known_good_slot
        self.state.candidate_slot = None
        self._start(target.artifact)
        # The outgoing morph may have left the fan running while the incoming
        # artifact drives nothing at all. The supervisor takes over anything
        # nobody owns — immediately here rather than a tick later, so the
        # handover has no gap.
        self.supervisor.manage_unowned_actuators(self.world.metrics(), self.world.time_s)
        self._save()
        self._record(
            "reverted",
            f"slot {outgoing} -> {self.state.active_slot} "
            f"(manifest={target.manifest_id}): {reason}",
            tick,
        )
        return True

    # --- restart -------------------------------------------------------------

    def restart(self) -> "VirtualDevice":
        """Simulate the supervisor process restarting.

        A fresh instance over the same world and the same state file. Anything
        the device forgets here is something it did not really persist.
        """
        log.info("device %s restarting", self.device_id)
        return VirtualDevice(
            device_id=self.device_id,
            world=self.world,
            schema=self.schema,
            registry=self.registry,
            state_path=self.state_path,
            policy=self.supervisor.policy,
            telemetry_sink=self.telemetry_sink,
            wall_clock=self.wall_clock,
            signing_key=self.signing_key,
        )

    # --- bookkeeping ---------------------------------------------------------

    def _save(self) -> None:
        slot_store.save_state(self.state, self.state_path)

    def _record(self, event: str, detail: str, tick: int | None = None) -> None:
        self.events.append((tick if tick is not None else self.world.tick, f"{event}: {detail}"))
        log.info("[%s] %s: %s", self.device_id, event, detail)

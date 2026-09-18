"""Contract-constrained pipeline: proposal → device, with every gate in between.

RESEARCH.md §1. The `manifest_compiler` counterpart of `orchestrator.py`, and
deliberately a separate module: the baseline is the experimental control and is
not edited to accommodate the mode being compared against it.

The order, with no path that skips a step:

    propose (LLM or stub, untrusted)
      -> validate   deterministic, against this device's schema and registry
      -> compile    templates only, byte-identical for identical input
      -> verify     closed-loop simulation across seeded scenarios
      -> sign       HMAC over the whole package
      -> deliver    the device verifies it again, locally, and decides
      -> observe    activation is recorded when the device says so, not before

Two properties of that list are load-bearing and are tested:

  - **A failed verification never reaches the signer.** The ledger's transition
    graph makes it unrepresentable, and this module never tries.
  - **Delivery is not activation.** The pipeline records `delivery_attempted`
    when it hands a package over and `accepted_by_device` only on the device's
    own verdict; `active_on_device` waits for probation to finish.

No model is reachable after the proposal step. Validation, compilation,
verification, signing and rollback are all deterministic (NFR-4).
"""

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import config
from server.compiler.compiler import CompilerReport, compile_manifest
from server.compiler.program import ControllerProgram
from server.deploy import ledger, rollback
from server.deploy.ledger import State
from server.manifest.models import BehaviorManifest
from server.manifest.registry import CapabilityRegistry, load_registry
from server.manifest.validator import ManifestValidationResult, validate
from server.ota import package as ota
from server.ota.package import FirmwarePackage
from server.schemas import AdaptationMode, AgentTask, TriggerType, load_hardware_schema
from server.verify.verifier import VerificationSuite, verify

log = logging.getLogger("caef.pipeline")


class DeliveryChannel(Protocol):
    """How a signed package reaches a device.

    An interface for the same reason the Distributor is one (TDD.md §2.3): the
    v0.1 implementation hands the package to an in-process virtual device, and
    a real one would put it on a socket, without either end changing.
    """

    def running_artifact_hash(self) -> str | None: ...

    def deliver(self, package: FirmwarePackage): ...


@dataclass
class PipelineResult:
    """What happened, and how far it got."""

    task_id: str
    device_id: str
    deployment_id: str | None = None
    state: State | None = None
    stage: str = "proposed"
    manifest: BehaviorManifest | None = None
    validation: ManifestValidationResult | None = None
    compilation: CompilerReport | None = None
    verification: VerificationSuite | None = None
    package: FirmwarePackage | None = None
    program: ControllerProgram | None = None
    accepted: bool = False
    reason: str | None = None
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    timeline: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.timeline.append(message)


class ManifestPipeline:
    """Drives one Task through the contract-constrained path."""

    def __init__(
        self,
        agent,
        channel: DeliveryChannel,
        registry: CapabilityRegistry | None = None,
        verification_seeds: list[int] | None = None,
        signing_key: bytes | None = None,
        clock=lambda: int(time.time()),
    ) -> None:
        self.agent = agent
        self.channel = channel
        self.registry = registry or load_registry()
        self.verification_seeds = verification_seeds or [config.SIM_DEFAULT_SEED]
        self.signing_key = signing_key
        self.clock = clock

    # --- the Distributor's contract ------------------------------------------

    async def handle(self, task: AgentTask) -> PipelineResult:
        """Same signature the baseline Orchestrator exposes, so either can be
        the Distributor's handler."""
        return await asyncio.to_thread(self.run, task)

    # --- the pipeline --------------------------------------------------------

    def run(self, task: AgentTask) -> PipelineResult:
        result = PipelineResult(task_id=task.task_id, device_id=task.device_id)

        if rollback.generation_halted(task.device_id):
            # Halted means halted until an operator clears it, and the check is
            # before the model for the same reason as in the baseline
            # (SAFETY_PROTOCOL.md §5.1).
            result.reason = "generation halted for this device"
            result.stage = "halted"
            result.note(f"dropped: {result.reason}")
            log.warning("generation halted for %s; dropping %s", task.device_id, task.task_id)
            return result

        if task.trigger_type is TriggerType.CONTEXT_TRIGGER:
            live = ledger.live_for_event(task.device_id, task.event)
            if live is not None:
                result.reason = f"already adapting to {task.event} ({live.manifest_id})"
                result.stage = "duplicate"
                result.note(f"dropped: {result.reason}")
                log.info("%s already adapting to %s; dropping duplicate", task.device_id, task.event)
                return result

        schema = load_hardware_schema(task.device_id)
        firmware_hash = self._current_firmware_hash(task)

        deployment_id = ledger.open_deployment(
            device_id=task.device_id,
            mode=AdaptationMode.MANIFEST_COMPILER,
            event_id=task.event_id,
            base_firmware_hash=firmware_hash,
            device_event_time=task.raw_payload.get("timestamp"),
            capability_registry_version=self.registry.capability_registry_version,
            detail=f"{task.trigger_type.value} {task.event}",
        )
        result.deployment_id = deployment_id
        result.state = State.PROPOSED
        result.note(f"proposed: deployment {deployment_id[:8]} for {task.event}")

        # 1. Propose. Everything after this line is deterministic.
        proposal = self._propose(task, schema, firmware_hash)
        result.attempts = proposal.attempts
        result.prompt_tokens = proposal.prompt_tokens
        result.completion_tokens = proposal.completion_tokens
        if not proposal.ok:
            return self._reject(
                result, "proposal", "; ".join(proposal.errors) or "no manifest proposed"
            )
        result.manifest = proposal.manifest
        result.note(f"manifest {proposal.manifest.manifest_id} proposed")

        # 2. Validate.
        validation = validate(proposal.manifest, self.registry, schema)
        result.validation = validation
        if validation.status == "fail":
            return self._reject(result, "validation", validation.reason or "invalid manifest")
        ledger.advance(
            deployment_id,
            State.MANIFEST_VALIDATED,
            manifest_id=proposal.manifest.manifest_id,
            manifest_hash=proposal.manifest.manifest_hash,
        )
        result.state = State.MANIFEST_VALIDATED
        result.note("manifest validated")

        # 3. Compile.
        compilation = compile_manifest(proposal.manifest, self.registry, schema)
        result.compilation = compilation.report
        if compilation.status == "fail":
            return self._reject(
                result, "compilation", compilation.report.reason or "compilation refused"
            )
        program = compilation.program
        result.program = program
        ledger.advance(
            deployment_id, State.COMPILED, artifact_hash=program.artifact_hash
        )
        result.state = State.COMPILED
        result.note(f"compiled {compilation.report.pattern} -> {program.artifact_hash[:12]}")

        # 4. Verify in the closed loop. Nothing gets signed after a failure here.
        suite = verify(
            program,
            schema,
            self.registry,
            seeds=self.verification_seeds,
            expect_cooling=self._expects_cooling(task),
        )
        result.verification = suite
        if suite.status == "fail":
            return self._reject(result, "verification", suite.summary())
        ledger.advance(deployment_id, State.SIMULATION_VERIFIED, detail=suite.summary())
        result.state = State.SIMULATION_VERIFIED
        result.note(f"verified: {suite.summary()}")

        # 5. Sign.
        sequence = ledger.next_sequence(task.device_id)
        package = ota.build_package(
            program,
            sequence_number=sequence,
            issued_at=self.clock(),
            lease_duration_seconds=proposal.manifest.maximum_duration_seconds,
            key=self.signing_key,
        )
        result.package = package
        ledger.advance(
            deployment_id,
            State.SIGNED,
            sequence_number=sequence,
            lease_duration_seconds=package.lease_duration_seconds,
        )
        result.state = State.SIGNED
        result.note(f"signed seq={sequence} lease={package.lease_duration_seconds}s")

        # 6. Deliver. A send is not an arrival.
        ledger.advance(deployment_id, State.DELIVERY_ATTEMPTED)
        result.state = State.DELIVERY_ATTEMPTED
        result.note("delivery attempted")

        outcome = self.channel.deliver(package)
        if outcome is None or not outcome.accepted:
            reason = (
                "device unreachable"
                if outcome is None
                else f"{outcome.verdict.rejection}: {outcome.reason}"
            )
            return self._reject(result, "delivery", reason)

        ledger.advance(
            deployment_id,
            State.ACCEPTED_BY_DEVICE,
            detail=f"installed into slot {outcome.slot} for probation",
        )
        result.state = State.ACCEPTED_BY_DEVICE
        result.accepted = True
        result.note(f"accepted by device into slot {outcome.slot}, on probation")
        log.info(
            "deployment %s accepted by %s (slot %s, lease %ss)",
            deployment_id,
            task.device_id,
            outcome.slot,
            outcome.lease_seconds,
        )
        return result

    # --- device-reported outcomes -------------------------------------------

    def record_activation(self, deployment_id: str, device_event_time: int | None = None) -> None:
        """The device finished probation. Only now is anything `active`."""
        ledger.advance(
            deployment_id,
            State.ACTIVE_ON_DEVICE,
            detail="probation passed",
            device_event_time=device_event_time,
        )

    def record_reversion(
        self, deployment_id: str, reason: str, device_event_time: int | None = None
    ) -> None:
        ledger.advance(
            deployment_id, State.REVERTED, detail=reason, device_event_time=device_event_time
        )

    def record_rollback(
        self, deployment_id: str, reason: str, device_event_time: int | None = None
    ) -> None:
        ledger.advance(
            deployment_id, State.ROLLED_BACK, detail=reason, device_event_time=device_event_time
        )

    # --- helpers -------------------------------------------------------------

    def _propose(self, task: AgentTask, schema, firmware_hash: str):
        """Call whichever agent is wired in.

        The LLM agent takes the validator so it can retry against a real reason;
        the stub does not retry because it is deterministic and would produce
        the same manifest again.
        """
        parameters = inspect.signature(self.agent.propose).parameters
        if "validate" in parameters:
            return self.agent.propose(
                task, schema, firmware_hash, validate=lambda m: validate(m, self.registry, schema)
            )
        return self.agent.propose(task, schema, firmware_hash)

    def _current_firmware_hash(self, task: AgentTask) -> str:
        """What the device says it is running, falling back to what we can see.

        The device's own report is the right input — and it is re-checked on the
        device, so a device that misreports only misleads itself into rejecting
        the package it is sent (RESEARCH.md §9).
        """
        reported = task.raw_payload.get("current_state_hash")
        if isinstance(reported, str) and reported and reported != "unknown":
            return reported
        return self.channel.running_artifact_hash() or "0" * 16

    @staticmethod
    def _expects_cooling(task: AgentTask) -> bool:
        """Whether this situation demands an actuator response.

        A context trigger naming heat does; a crash report does not, and holding
        a monitoring contract to a cooling latency would be nonsense.
        """
        return task.trigger_type is TriggerType.CONTEXT_TRIGGER and "HEAT" in task.event.upper()

    def _reject(self, result: PipelineResult, stage: str, reason: str) -> PipelineResult:
        result.stage = stage
        result.reason = reason
        result.accepted = False
        if result.deployment_id:
            ledger.advance(result.deployment_id, State.REJECTED, detail=f"{stage}: {reason}")
            result.state = State.REJECTED
        result.note(f"rejected at {stage}: {reason}")
        log.info("deployment %s rejected at %s: %s", result.deployment_id, stage, reason)
        return result


class LocalDeliveryChannel:
    """Hands packages to an in-process `VirtualDevice`.

    `online` models the link, not the device: turning it off makes delivery fail
    the way an unreachable device does, while the device keeps ticking, keeps
    its lease and keeps its ability to revert (RESEARCH.md §8).
    """

    def __init__(self, device) -> None:
        self.device = device
        self.online = True
        self.attempts: list[FirmwarePackage] = []

    def running_artifact_hash(self) -> str | None:
        return self.device.running_artifact_hash

    def deliver(self, package: FirmwarePackage):
        self.attempts.append(package)
        if not self.online:
            log.warning("delivery to %s failed: link is down", self.device.device_id)
            return None
        return self.device.install(package)

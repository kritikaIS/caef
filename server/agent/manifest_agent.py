"""Manifest-mode Agent: a deterministic stub and a real LLM proposer.

RESEARCH.md §1/§2. Both produce the same artifact — a `BehaviorManifest` — and
neither is trusted: whatever comes out of here goes to the validator, the
compiler and the closed-loop verifier before it can reach a device.

`StubManifestAgent` is what makes the whole safe-mode demo and test suite run
with no API key. It is not a mock in the "returns a canned blob" sense: it takes
the same task, reads the same registry, and produces a real manifest that goes
through every downstream gate. Its `variant` parameter lets the experiment
harness ask for *deliberately flawed* proposals, which is how the arms are
compared on the same distribution of model behaviour rather than on the happy
path only (RESEARCH.md §12).

`LLMManifestAgent` wires a real model over LangChain. It retries on rejection
with the deterministic reason fed back, sharing the same `MAX_RETRIES` budget as
the baseline Agent (SAFETY_PROTOCOL.md §4). It has been exercised against a
scripted fake in the test suite; it has not been run against a live model in
this repository, and nothing here claims otherwise.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import config
from server.agent import manifest_prompts as prompts
from server.manifest.models import BehaviorManifest
from server.manifest.registry import CapabilityRegistry
from server.schemas import AgentTask, HardwareSchema, TriggerType

log = logging.getLogger("caef.agent.manifest")

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ProposalError(ValueError):
    """The model returned something that is not a manifest.

    A failed attempt, not a pipeline crash: a bad response must not take down
    the server, and it burns retry budget like any other rejection.
    """


@dataclass
class ManifestProposal:
    """One proposal plus the trail of what it took to get there."""

    manifest: BehaviorManifest | None
    attempts: int = 1
    errors: list[str] = field(default_factory=list)
    raw_responses: list[str] = field(default_factory=list)
    # Populated when a real model was used; zero for the stub.
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.manifest is not None


class StubVariant(StrEnum):
    """What kind of proposal the stub should make.

    Each flawed variant is a mistake a real model plausibly makes, and each is
    caught by a *different* gate — which is what makes them useful as an
    experimental distribution rather than as decoration.
    """

    SOUND = "sound"  # a correct cooling contract
    UNKNOWN_CAPABILITY = "unknown_capability"  # invents a capability: validator
    OVERLONG_LEASE = "overlong_lease"  # lease beyond the ceiling: validator
    OVERLAPPING_CONDITIONS = "overlapping_conditions"  # ambiguous rules: validator
    UNDECLARED_ACTUATOR = "undeclared_actuator"  # drives what it did not declare: validator
    UNSUPPORTED_COMBINATION = "unsupported_combination"  # no pattern covers it: compiler
    THRESHOLD_TOO_HIGH = "threshold_too_high"  # legal, useless: verifier
    NO_COOLING = "no_cooling"  # answers heat with monitoring: verifier


# The capability set a sound thermal contract asks for. Ordered for readability;
# the compiler sorts them, so order has no effect on the artifact.
COOLING_CAPABILITIES = [
    "read_temperature",
    "fan_on",
    "fan_off",
    "emit_heartbeat",
    "emit_context_event",
    "emit_critical_event",
    "enter_safe_idle",
]
MONITOR_CAPABILITIES = ["read_temperature", "emit_heartbeat", "emit_context_event"]


class StubManifestAgent:
    """Deterministic proposer. No network, no API key, no randomness."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        variant: StubVariant | str = StubVariant.SOUND,
    ) -> None:
        self.registry = registry
        self.variant = StubVariant(variant)

    def propose(
        self,
        task: AgentTask,
        schema: HardwareSchema,
        current_firmware_hash: str,
    ) -> ManifestProposal:
        payload = self._payload(task, current_firmware_hash)
        try:
            return ManifestProposal(manifest=BehaviorManifest.model_validate(payload))
        except ValueError as exc:
            # A variant whose flaw the *parser* catches rather than the
            # validator. Still a proposal that was made and rejected, and the
            # experiment counts it as one.
            return ManifestProposal(manifest=None, errors=[str(exc)])

    # --- payload construction ------------------------------------------------

    def _payload(self, task: AgentTask, firmware_hash: str) -> dict[str, Any]:
        data = task.raw_payload.get("data", {})
        threshold = float(data.get("threshold", config.HEAT_THRESHOLD_C))
        recovery = float(config.REVERSION_RECOVERY_THRESHOLD_C)
        lease = min(config.REVERSION_WINDOW_SECONDS, config.MAX_LEASE_SECONDS)

        manifest = {
            "manifest_version": config.MANIFEST_VERSION,
            "manifest_id": self._manifest_id(task),
            "device_id": task.device_id,
            "event_id": task.event_id,
            "trigger_type": task.trigger_type.value,
            "trigger_event": task.event,
            "current_firmware_hash": firmware_hash,
            "capability_registry_version": self.registry.capability_registry_version,
            "requested_capabilities": list(COOLING_CAPABILITIES),
            "sensor_inputs": ["temperature_c"],
            "actuator_outputs": ["fan"],
            "activation_condition": {
                "metric": "temperature_c",
                "operator": ">=",
                "value": threshold,
            },
            "recovery_condition": {
                "metric": "temperature_c",
                "operator": "<",
                "value": recovery,
            },
            "maximum_duration_seconds": lease,
            "control_period_seconds": config.SIM_TICK_SECONDS,
            "resource_budget": self._budget(COOLING_CAPABILITIES),
            "fallback_behavior": "enter_safe_idle",
            "rationale": (
                f"{task.event}: hold cooling while temperature is at or above "
                f"{threshold}C and release it below {recovery}C. Lease {lease}s."
            ),
        }
        if task.trigger_type is TriggerType.CRITICAL_FAILURE:
            # A crash report is answered with a monitoring contract: the point is
            # to keep the device observable and out of trouble, not to guess at a
            # fix in a language that cannot express one.
            manifest.update(
                requested_capabilities=list(MONITOR_CAPABILITIES),
                actuator_outputs=[],
                fallback_behavior="restore_previous_firmware",
                resource_budget=self._budget(MONITOR_CAPABILITIES),
                rationale=(
                    f"{task.event}: fall back to monitoring and reporting while the "
                    "fault is investigated."
                ),
            )
        return self._apply_variant(manifest)

    def _manifest_id(self, task: AgentTask) -> str:
        """Derived, never random: the same task always proposes the same id, so
        an experiment run is reproducible down to the artifact hash."""
        stem = re.sub(r"[^A-Za-z0-9]", "", task.event_id)[:16] or "seed"
        return f"m-{stem}-{self.variant.value}"[:64]

    def _budget(self, capabilities: list[str]) -> dict[str, Any]:
        cpu = sum(
            self.registry.require(name).resource_cost.cpu_ms_per_step
            for name in capabilities
            if self.registry.get(name)
        )
        memory = sum(
            self.registry.require(name).resource_cost.memory_kb
            for name in capabilities
            if self.registry.get(name)
        )
        return {
            # Headroom, the way a careful proposer would leave some.
            "max_cpu_ms_per_step": round(cpu * 2 + 1.0, 3),
            "max_memory_kb": int(memory * 2 + 16),
            "max_actuator_transitions_per_minute": (
                config.VERIFY_MAX_ACTUATOR_TRANSITIONS_PER_MIN
            ),
        }

    def _apply_variant(self, manifest: dict[str, Any]) -> dict[str, Any]:
        match self.variant:
            case StubVariant.SOUND:
                return manifest
            case StubVariant.UNKNOWN_CAPABILITY:
                manifest["requested_capabilities"] = [
                    *manifest["requested_capabilities"][:5],
                    "gpio_write_raw",
                ]
            case StubVariant.OVERLONG_LEASE:
                manifest["maximum_duration_seconds"] = config.MAX_LEASE_SECONDS * 4
            case StubVariant.OVERLAPPING_CONDITIONS:
                manifest["recovery_condition"] = {
                    "metric": "temperature_c",
                    "operator": ">",
                    "value": 40.0,
                }
            case StubVariant.UNDECLARED_ACTUATOR:
                manifest["actuator_outputs"] = []
            case StubVariant.UNSUPPORTED_COMBINATION:
                # Individually legal, jointly unrecognised: every capability is
                # registered and every precondition holds, but no compilation
                # pattern covers "cool the device *and* range-find". This is the
                # one that has to reach the compiler to be refused.
                manifest["requested_capabilities"] = [
                    "read_temperature",
                    "read_distance",
                    "fan_on",
                    "fan_off",
                    "emit_heartbeat",
                ]
                manifest["sensor_inputs"] = ["temperature_c", "distance_cm"]
                manifest["fallback_behavior"] = "restore_previous_firmware"
            case StubVariant.THRESHOLD_TOO_HIGH:
                manifest["activation_condition"] = {
                    "metric": "temperature_c",
                    "operator": ">=",
                    "value": 200.0,
                }
            case StubVariant.NO_COOLING:
                manifest["requested_capabilities"] = list(MONITOR_CAPABILITIES)
                manifest["actuator_outputs"] = []
                manifest["fallback_behavior"] = "restore_previous_firmware"
                manifest["resource_budget"] = self._budget(MONITOR_CAPABILITIES)
        return manifest


class LLMManifestAgent:
    """Proposes a manifest with a real model, retrying on deterministic rejection.

    The model is injected rather than constructed, so the tests drive it with a
    scripted fake and no network (TDD.md §5). `validate` is the caller's
    deterministic validator, passed in so this module never decides for itself
    whether a proposal is acceptable.
    """

    def __init__(self, llm, registry: CapabilityRegistry, max_retries: int | None = None) -> None:
        self.llm = llm
        self.registry = registry
        self.max_retries = max_retries or config.MAX_RETRIES

    def propose(
        self,
        task: AgentTask,
        schema: HardwareSchema,
        current_firmware_hash: str,
        validate=None,
    ) -> ManifestProposal:
        contract = prompts.output_contract(
            task, self.registry, current_firmware_hash, config.MAX_LEASE_SECONDS
        )
        messages: list = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": prompts.initial_prompt(
                    task,
                    schema,
                    self.registry,
                    current_firmware_hash,
                    config.MAX_LEASE_SECONDS,
                    config.EMERGENCY_TEMP_C,
                ),
            },
        ]
        proposal = ManifestProposal(manifest=None, attempts=0)

        for attempt in range(1, self.max_retries + 1):
            proposal.attempts = attempt
            reply = self.llm.invoke(messages)
            messages.append(reply)
            text = _text_of(reply)
            proposal.raw_responses.append(text)
            self._count_tokens(reply, proposal)

            try:
                manifest = parse_manifest(text)
            except ProposalError as exc:
                proposal.errors.append(str(exc))
                messages.append(
                    {
                        "role": "user",
                        "content": prompts.retry_prompt(
                            str(exc), attempt, self.max_retries, contract
                        ),
                    }
                )
                continue

            if validate is not None:
                verdict = validate(manifest)
                if verdict.status == "fail":
                    proposal.errors.append(verdict.reason or "validation failed")
                    messages.append(
                        {
                            "role": "user",
                            "content": prompts.retry_prompt(
                                verdict.reason or "validation failed",
                                attempt,
                                self.max_retries,
                                contract,
                            ),
                        }
                    )
                    continue

            proposal.manifest = manifest
            return proposal

        log.warning("manifest proposal exhausted %s attempts for %s", self.max_retries, task.event_id)
        return proposal

    @staticmethod
    def _count_tokens(reply, proposal: ManifestProposal) -> None:
        """Record token use when the provider reports it, so the experiment can
        report real numbers and leave them at zero when it cannot."""
        usage = getattr(reply, "usage_metadata", None) or {}
        proposal.prompt_tokens += int(usage.get("input_tokens", 0) or 0)
        proposal.completion_tokens += int(usage.get("output_tokens", 0) or 0)


def parse_manifest(raw: str) -> BehaviorManifest:
    """Extract and validate a manifest from a model reply.

    Every failure here is a `ProposalError`, including a schema violation: the
    strict model *is* the first gate, and "unknown field" is a rejection with a
    reason worth feeding back.
    """
    text = raw.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProposalError(f"response was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProposalError("response was not a JSON object")
    try:
        return BehaviorManifest.model_validate(payload)
    except ValueError as exc:
        raise ProposalError(f"not a valid Behavior Manifest: {exc}") from exc


def _text_of(reply) -> str:
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)

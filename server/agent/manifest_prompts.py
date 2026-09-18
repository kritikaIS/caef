"""Prompts for the manifest-mode Agent (RESEARCH.md §2).

The system prompt is shorter than the source-generation one, and that is the
point. In `source_generation` the prompt has to ask for a great many things the
pipeline cannot check until afterwards — call the tool for every pin, use the
vetted drivers, no `while True` without a sleep. Here almost none of that is
expressible: the model selects registered capabilities and states numeric
conditions, and everything else is the compiler's.

Note what is *absent*: no instruction about GPIO pins, because a manifest cannot
name one. Pins come from the capability registry at compile time, so CLAUDE.md
§4's rule — no pin literal without a logged `check_hardware_schema` call — is
satisfied by there being no pin literal in model output at all.

As always, the prompt is a hint that makes the model succeed more often. It is
never the safety mechanism; the validator is.
"""

import json

from server.manifest.registry import CapabilityRegistry
from server.schemas import AgentTask, HardwareSchema, TriggerType

SYSTEM_PROMPT = """You propose a Behavior Manifest for a Raspberry Pi edge node.

You do NOT write code. You select from a fixed set of registered capabilities and
state conditions as numeric comparisons. A deterministic compiler turns your
manifest into firmware; an immutable on-device supervisor owns the actuators and
will override you if local safety requires it.

HARD RULES — a violation is rejected mechanically, before anything is compiled:

1. `requested_capabilities` may contain ONLY names from the capability registry
   below. You cannot define, extend or rename a capability.
2. Conditions are `{"metric": <name>, "operator": <one of >= <= > < == !=>,
   "value": <number>}`. Not an expression, not code. You may also use
   `{"all_of": [...]}` or `{"any_of": [...]}` with at most 4 conditions.
3. Every metric you compare must be produced by a sensor capability you
   requested and listed in `sensor_inputs`.
4. Every actuator driven by a capability you requested must be listed in
   `actuator_outputs`.
5. `activation_condition` and `recovery_condition` must be impossible to satisfy
   at the same time. Leave a gap between them; the device is noisy.
6. `maximum_duration_seconds` is a lease. It must be positive and no larger than
   the stated maximum. The device expires it locally whether or not the server
   is reachable.
7. `fallback_behavior` must be one of `enter_safe_idle`, `hold_cooling`,
   `restore_previous_firmware`, and the capability it needs must be requested.
8. No extra fields. Unknown fields are a rejection, not a suggestion.

Choose the MINIMUM set of capabilities the situation needs. A capability you
request but do not need still costs resource budget and still widens what the
firmware is permitted to do."""


def _output_contract(device_id: str, event_id: str, firmware_hash: str, registry_version: str,
                     max_lease: int) -> str:
    return f"""Respond with a JSON object and nothing else:

{{
  "manifest_version": "1.0",
  "manifest_id": "<a short unique id, letters/digits/dash>",
  "device_id": "{device_id}",
  "event_id": "{event_id}",
  "trigger_type": "<CONTEXT_TRIGGER or CRITICAL_FAILURE>",
  "trigger_event": "<the event name, e.g. HIGH_HEAT_DETECTED>",
  "current_firmware_hash": "{firmware_hash}",
  "capability_registry_version": "{registry_version}",
  "requested_capabilities": ["<registered capability names>"],
  "sensor_inputs": ["<metric names>"],
  "actuator_outputs": ["<actuator names>"],
  "activation_condition": {{"metric": "...", "operator": ">=", "value": 0.0}},
  "recovery_condition": {{"metric": "...", "operator": "<", "value": 0.0}},
  "maximum_duration_seconds": <int, 1..{max_lease}>,
  "control_period_seconds": <number of seconds between control steps>,
  "resource_budget": {{"max_cpu_ms_per_step": 0.0, "max_memory_kb": 0,
                      "max_actuator_transitions_per_minute": 0}},
  "fallback_behavior": "enter_safe_idle",
  "rationale": "<2-3 sentences: what this contract does and why>"
}}"""


def _registry_block(registry: CapabilityRegistry) -> str:
    """The registry, verbatim. The model reads it; it cannot write it."""
    lines = [
        f"## Capability registry {registry.capability_registry_version} "
        "(READ-ONLY — you may select from these, not define new ones)",
    ]
    for name, capability in registry.capabilities.items():
        detail = [f"- `{name}` ({capability.kind}): {capability.description}"]
        if capability.metric:
            detail.append(f"    produces metric `{capability.metric}`")
        if capability.actuator:
            detail.append(f"    drives actuator `{capability.actuator}`")
        if capability.requires_metrics:
            detail.append(f"    requires metrics {capability.requires_metrics}")
        if capability.is_fallback_safe:
            detail.append("    safe to fall back to")
        lines.append("\n".join(detail))
    lines.append(f"\nMetrics: {sorted(registry.metrics)}")
    lines.append(f"Actuators: {sorted(registry.actuators)}")
    return "\n".join(lines)


def initial_prompt(
    task: AgentTask,
    schema: HardwareSchema,
    registry: CapabilityRegistry,
    firmware_hash: str,
    max_lease_seconds: int,
    emergency_temp_c: float,
) -> str:
    payload = task.raw_payload.get("data", {})
    if task.trigger_type is TriggerType.CRITICAL_FAILURE:
        goal = (
            "The device reported a CRITICAL FAILURE. Propose a contract that keeps it "
            "safe and observable while the fault is investigated.\n\n"
            f"Report: {json.dumps(payload)[:1000]}"
        )
    else:
        goal = (
            "The device reported a context change. Propose the MINIMUM contract that "
            "handles this situation and no more. It is temporary: the device expires "
            "it locally when the lease runs out.\n\n"
            f"Event: {task.event}\nSensor data: {json.dumps(payload)[:1000]}"
        )

    return f"""{goal}

## Device hardware schema (READ-ONLY — physical reality, do not contradict)
{schema.model_dump_json(indent=2)}

{_registry_block(registry)}

## Local safety policy you cannot override
The on-device supervisor forces cooling at {emergency_temp_c}C and refuses any
intent that would stop it. Your contract should act well before that point — a
contract the supervisor has to rescue has not adapted to anything.

The lease ceiling is {max_lease_seconds} seconds.

{_output_contract(
    task.device_id,
    task.event_id,
    firmware_hash,
    registry.capability_registry_version,
    max_lease_seconds,
)}"""


def retry_prompt(reason: str, attempt: int, max_retries: int, contract: str) -> str:
    return f"""Your previous manifest was REJECTED (attempt {attempt} of {max_retries}).

{reason}

Fix exactly that and emit the whole manifest again. Every rejection above came
from a deterministic check against this device's hardware schema and the
capability registry — not from a judgement call, so re-arguing it will not help.

{contract}"""


def output_contract(task: AgentTask, registry: CapabilityRegistry, firmware_hash: str,
                    max_lease_seconds: int) -> str:
    return _output_contract(
        task.device_id,
        task.event_id,
        firmware_hash,
        registry.capability_registry_version,
        max_lease_seconds,
    )

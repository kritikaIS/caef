"""Prevalidated templates — the only shapes the compiler may emit.

RESEARCH.md §4. Each registry capability names one of these four templates, and
a template turns *the registry's own declaration* into an IR fragment. Nothing
model-authored is copied through: the compiler reads the capability's pins,
limits and parameters out of the registry file and the manifest's already-parsed
typed fields, and never sees model text at all.

Adding a fifth behaviour to the system means adding a template here and a
registry entry that points at it — deliberately a code change with a review,
not something a manifest can reach.
"""

from dataclasses import dataclass

from server.compiler.program import IntentSpec, SensorBinding
from server.manifest.models import BehaviorManifest
from server.manifest.registry import Capability

# The one substitution a template parameter may request: the event name the
# manifest declared. Safe because `trigger_event` is pattern-validated at parse
# time (`^[A-Z][A-Z0-9_]{2,63}$`) — it is a label, and it is never evaluated.
TRIGGER_EVENT_BINDING = "@trigger_event"


class TemplateError(ValueError):
    """A registry entry asked for something its template cannot build."""


@dataclass(frozen=True)
class TemplateContext:
    manifest: BehaviorManifest


def _resolve(value: str, context: TemplateContext) -> str:
    return context.manifest.trigger_event if value == TRIGGER_EVENT_BINDING else value


def _single_pin(name: str, capability: Capability) -> int:
    if len(capability.permitted_pins) != 1:
        raise TemplateError(
            f"{name}: this template needs exactly one permitted pin, "
            f"got {capability.permitted_pins}"
        )
    return capability.permitted_pins[0]


def sensor_read(name: str, capability: Capability, context: TemplateContext) -> SensorBinding:
    if not capability.metric:
        raise TemplateError(f"{name}: a sensor capability must declare its metric")
    return SensorBinding(
        capability=name, metric=capability.metric, pin=_single_pin(name, capability)
    )


def actuator_set_state(
    name: str, capability: Capability, context: TemplateContext
) -> IntentSpec:
    limits = capability.actuator_limits
    if limits is None:
        raise TemplateError(f"{name}: an actuator capability must declare actuator_limits")
    return IntentSpec(
        capability=name,
        kind="actuator",
        actuator=limits.actuator,
        state=limits.state,
        pin=_single_pin(name, capability),
        current_ma=limits.current_ma,
        min_hold_seconds=limits.min_hold_seconds,
        max_transitions_per_minute=limits.max_transitions_per_minute,
    )


def telemetry_emit(name: str, capability: Capability, context: TemplateContext) -> IntentSpec:
    params = capability.template_params
    event = _resolve(params.get("event", ""), context)
    if not event:
        raise TemplateError(f"{name}: telemetry template needs an event name")
    return IntentSpec(
        capability=name,
        kind="telemetry",
        event=event,
        trigger_type=params.get("trigger_type", "CONTEXT_TRIGGER"),
    )


def safe_idle(name: str, capability: Capability, context: TemplateContext) -> IntentSpec:
    return IntentSpec(capability=name, kind="safety")


TEMPLATES = {
    "sensor_read": sensor_read,
    "actuator_set_state": actuator_set_state,
    "telemetry_emit": telemetry_emit,
    "safe_idle": safe_idle,
}


def build(name: str, capability: Capability, context: TemplateContext):
    builder = TEMPLATES.get(capability.template)
    if builder is None:
        raise TemplateError(f"{name}: no template named {capability.template!r}")
    return builder(name, capability, context)

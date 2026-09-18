"""M20 — the contract-constrained pipeline end to end (RESEARCH.md §1).

Two claims are under test here. First, that every gate is on the path and each
one catches the class of mistake it exists for — the stub agent's flawed
variants are rejected at the validator, the compiler and the verifier
respectively, and none of them reaches a device. Second, that the ledger tells
the truth: a package that was sent is not recorded as running, and a package
that failed verification is never signed at all.

Nothing here needs an API key.
"""

import logging
import tempfile
from pathlib import Path

import pytest

import config
from edge_node.virtual_device import VirtualDevice
from server.agent.manifest_agent import (
    LLMManifestAgent,
    ProposalError,
    StubManifestAgent,
    StubVariant,
    parse_manifest,
)
from server.compiler.compiler import compile_manifest
from server.db.models import Device, Event, SessionLocal
from server.deploy import ledger
from server.deploy.ledger import State
from server.manifest.registry import load_registry
from server.manifest_pipeline import LocalDeliveryChannel, ManifestPipeline
from server.schemas import AgentTask, TriggerType, load_hardware_schema
from server.sim import scenarios
from server.sim.world import ThermalWorld
from tests.fakes import FakeLLM, FakeReply
from tests.fixtures import manifests

DEVICE = "pi_node_alpha"
KEY = b"\x09" * 32


@pytest.fixture(autouse=True)
def quiet():
    for name in ("caef.supervisor", "caef.device", "caef.ota", "caef.slots",
                 "caef.ledger", "caef.pipeline"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    yield
    for name in ("caef.supervisor", "caef.device", "caef.ota", "caef.slots",
                 "caef.ledger", "caef.pipeline"):
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture
def registry():
    return load_registry("1.0.0")


@pytest.fixture
def schema():
    return load_hardware_schema(DEVICE)


@pytest.fixture
def baseline(registry, schema):
    return compile_manifest(
        manifests.monitor(
            requested_capabilities=["read_temperature", "emit_heartbeat", "emit_context_event"],
            trigger_event="HIGH_HEAT_DETECTED",
            current_firmware_hash="0" * 16,
        ),
        registry,
        schema,
    ).program


@pytest.fixture
def device(tmp_path, registry, schema, baseline):
    world = ThermalWorld(scenarios.get("gradual_overheat").world)
    node = VirtualDevice(
        device_id=DEVICE,
        world=world,
        schema=schema,
        registry=registry,
        state_path=tmp_path / "device.json",
        signing_key=KEY,
    )
    node.provision(baseline)
    with SessionLocal() as db:
        db.add(Device(id=DEVICE, mcu_type="RaspberryPi_4B"))
        db.commit()
    return node


@pytest.fixture
def channel(device):
    return LocalDeliveryChannel(device)


def make_pipeline(agent, channel, registry, seeds=(1,)):
    return ManifestPipeline(
        agent, channel, registry, verification_seeds=list(seeds), signing_key=KEY
    )


def heat_task(device, event_id="event-heat-1") -> AgentTask:
    with SessionLocal() as db:
        if db.get(Event, event_id) is None:
            db.add(
                Event(
                    id=event_id,
                    device_id=DEVICE,
                    trigger_type=TriggerType.CONTEXT_TRIGGER,
                    event="HIGH_HEAT_DETECTED",
                    timestamp=171542000,
                    current_state_hash=device.running_artifact_hash,
                )
            )
            db.commit()
    return AgentTask(
        task_id=f"task-{event_id}",
        event_id=event_id,
        device_id=DEVICE,
        trigger_type=TriggerType.CONTEXT_TRIGGER,
        event="HIGH_HEAT_DETECTED",
        raw_payload={
            "data": {"temp_c": 85.4, "threshold": config.HEAT_THRESHOLD_C},
            "current_state_hash": device.running_artifact_hash,
            "timestamp": 171542000,
        },
    )


# --- the happy path ----------------------------------------------------------


def test_a_sound_proposal_reaches_the_device(device, channel, registry):
    pipeline = make_pipeline(StubManifestAgent(registry), channel, registry)
    for _ in range(28):
        device.tick()

    result = pipeline.run(heat_task(device))

    assert result.accepted is True
    assert result.state is State.ACCEPTED_BY_DEVICE
    assert result.validation.status == "pass"
    assert result.compilation.status == "pass"
    assert result.verification.status == "pass"
    assert result.package is not None
    assert device.state.candidate_status == "probation"
    assert [
        transition.to_state for transition in ledger.transitions(result.deployment_id)
    ] == [
        State.PROPOSED,
        State.MANIFEST_VALIDATED,
        State.COMPILED,
        State.SIMULATION_VERIFIED,
        State.SIGNED,
        State.DELIVERY_ATTEMPTED,
        State.ACCEPTED_BY_DEVICE,
    ]


def test_activation_is_recorded_only_after_probation(device, channel, registry):
    pipeline = make_pipeline(StubManifestAgent(registry), channel, registry)
    for _ in range(28):
        device.tick()
    result = pipeline.run(heat_task(device))

    assert ledger.active_on_device(DEVICE) is None, "acceptance is not activation"

    for _ in range(config.PROBATION_HEALTHY_TICKS):
        device.tick()
    assert device.state.candidate_status == "active"

    pipeline.record_activation(result.deployment_id, device_event_time=171542100)
    live = ledger.active_on_device(DEVICE)
    assert live is not None and live.id == result.deployment_id
    assert live.device_event_time == 171542100


def test_the_morph_actually_cools_the_device(device, channel, registry):
    """The adaptation has to do something, not merely install."""
    pipeline = make_pipeline(StubManifestAgent(registry), channel, registry)
    for _ in range(28):
        device.tick()
    hot = device.world.device_temp_c
    assert hot > config.HEAT_THRESHOLD_C - 5

    assert pipeline.run(heat_task(device)).accepted
    for _ in range(15):
        device.tick()

    assert device.world.device_temp_c < hot - 10
    assert device.world.actuators["fan"] == "on" or device.world.device_temp_c < 70


# --- each gate catches its own class of mistake -----------------------------


@pytest.mark.parametrize(
    "variant,stage",
    [
        (StubVariant.UNKNOWN_CAPABILITY, "validation"),
        (StubVariant.OVERLONG_LEASE, "validation"),
        (StubVariant.OVERLAPPING_CONDITIONS, "validation"),
        (StubVariant.UNDECLARED_ACTUATOR, "validation"),
        (StubVariant.UNSUPPORTED_COMBINATION, "compilation"),
        (StubVariant.THRESHOLD_TOO_HIGH, "verification"),
        (StubVariant.NO_COOLING, "verification"),
    ],
)
def test_flawed_proposals_are_rejected_at_the_expected_gate(
    device, channel, registry, variant, stage
):
    pipeline = make_pipeline(StubManifestAgent(registry, variant), channel, registry)
    result = pipeline.run(heat_task(device))

    assert result.accepted is False
    assert result.stage == stage, result.reason
    assert result.state is State.REJECTED
    assert device.state.slots["B"].empty, "nothing may reach the device"
    assert ledger.active_on_device(DEVICE) is None


def test_a_failed_verification_is_never_signed(device, channel, registry):
    """The gate the whole design hangs on."""
    pipeline = make_pipeline(
        StubManifestAgent(registry, StubVariant.THRESHOLD_TOO_HIGH), channel, registry
    )
    result = pipeline.run(heat_task(device))

    assert result.stage == "verification"
    assert result.package is None, "an unverified artifact must never be signed"
    assert channel.attempts == [], "and must never be offered to a device"
    states = [transition.to_state for transition in ledger.transitions(result.deployment_id)]
    assert State.SIGNED not in states
    assert states[-1] is State.REJECTED


def test_a_rejected_proposal_still_leaves_an_audit_trail(device, channel, registry):
    pipeline = make_pipeline(
        StubManifestAgent(registry, StubVariant.UNKNOWN_CAPABILITY), channel, registry
    )
    result = pipeline.run(heat_task(device))
    deployment = ledger.get(result.deployment_id)
    assert deployment.state is State.REJECTED
    assert "gpio_write_raw" in deployment.reason


# --- delivery ----------------------------------------------------------------


def test_an_undeliverable_package_is_not_recorded_as_active(device, channel, registry):
    """The v0.1 bug, stated as a test: a push nobody answered is not a deploy."""
    channel.online = False
    pipeline = make_pipeline(StubManifestAgent(registry), channel, registry)

    result = pipeline.run(heat_task(device))

    assert result.accepted is False
    assert result.stage == "delivery"
    assert ledger.active_on_device(DEVICE) is None
    states = [transition.to_state for transition in ledger.transitions(result.deployment_id)]
    assert State.DELIVERY_ATTEMPTED in states
    assert State.ACCEPTED_BY_DEVICE not in states
    assert device.state.running_slot == "A"


def test_the_device_can_refuse_a_package_the_server_signed(device, channel, registry):
    """The device verifies independently; the server having signed is not the
    end of the argument."""
    device.signing_key = b"\xff" * 32  # a device that never got this server's key
    pipeline = make_pipeline(StubManifestAgent(registry), channel, registry)

    result = pipeline.run(heat_task(device))
    assert result.accepted is False
    assert "invalid_signature" in result.reason


def test_sequence_numbers_increase_across_deployments(device, channel, registry):
    pipeline = make_pipeline(StubManifestAgent(registry), channel, registry)
    first = pipeline.run(heat_task(device, "event-heat-1"))
    assert first.accepted
    for _ in range(config.PROBATION_HEALTHY_TICKS):
        device.tick()
    pipeline.record_activation(first.deployment_id)
    pipeline.record_reversion(first.deployment_id, "lease expired")
    device.revert_local("lease expired")

    second = pipeline.run(heat_task(device, "event-heat-2"))
    assert second.package.sequence_number > first.package.sequence_number


# --- the guards before the model --------------------------------------------


def test_a_halted_device_is_dropped_before_the_agent_is_reached(device, channel, registry):
    class ExplodingAgent:
        def propose(self, *args, **kwargs):
            raise AssertionError("the agent must not be reached for a halted device")

    with SessionLocal() as db:
        row = db.get(Device, DEVICE)
        row.generation_halted = True
        db.commit()

    pipeline = make_pipeline(ExplodingAgent(), channel, registry)
    result = pipeline.run(heat_task(device))
    assert result.stage == "halted"
    assert result.deployment_id is None


def test_a_repeated_trigger_for_a_live_situation_is_dropped(device, channel, registry):
    """A device that keeps re-reporting a situation it is already adapting to
    must not collect a second morph — and re-issuing would restart the lease
    (LOOPS.md §2a)."""
    pipeline = make_pipeline(StubManifestAgent(registry), channel, registry)
    first = pipeline.run(heat_task(device, "event-heat-1"))
    assert first.accepted

    second = pipeline.run(heat_task(device, "event-heat-2"))
    assert second.stage == "duplicate"
    assert second.deployment_id is None
    assert len(channel.attempts) == 1


# --- the LLM proposer, driven by a scripted fake ----------------------------


def manifest_reply(**overrides) -> FakeReply:
    import json

    payload = {**manifests.COOLING, **overrides}
    return FakeReply(content=json.dumps(payload))


def test_the_llm_agent_parses_a_manifest_from_a_fenced_reply(registry, schema):
    import json

    fenced = FakeReply(content="```json\n" + json.dumps(manifests.COOLING) + "\n```")
    agent = LLMManifestAgent(FakeLLM([fenced]), registry)
    proposal = agent.propose(_llm_task(), schema, manifests.COOLING["current_firmware_hash"])
    assert proposal.ok
    assert proposal.manifest.manifest_id == manifests.COOLING["manifest_id"]


def test_the_llm_agent_retries_with_the_deterministic_reason(registry, schema):
    """A rejection is fed back verbatim, exactly as the baseline Agent does with
    a Guard Rail reason (SAFETY_PROTOCOL.md §4)."""
    bad = manifest_reply(requested_capabilities=["read_temperature", "gpio_write_raw"])
    good = manifest_reply()
    llm = FakeLLM([bad, good])
    agent = LLMManifestAgent(llm, registry)

    from server.manifest.validator import validate

    proposal = agent.propose(
        _llm_task(),
        schema,
        manifests.COOLING["current_firmware_hash"],
        validate=lambda manifest: validate(manifest, registry, schema),
    )
    assert proposal.ok
    assert proposal.attempts == 2
    assert "gpio_write_raw" in llm.prompts_seen


def test_the_llm_agent_gives_up_within_its_retry_budget(registry, schema):
    llm = FakeLLM([FakeReply(content="I cannot do that.")] * config.MAX_RETRIES)
    proposal = LLMManifestAgent(llm, registry).propose(
        _llm_task(), schema, manifests.COOLING["current_firmware_hash"]
    )
    assert proposal.ok is False
    assert proposal.attempts == config.MAX_RETRIES
    assert len(proposal.errors) == config.MAX_RETRIES


def test_a_reply_with_an_unknown_field_is_a_proposal_error():
    import json

    with pytest.raises(ProposalError, match="not a valid Behavior Manifest"):
        parse_manifest(json.dumps({**manifests.COOLING, "run_shell": "rm -rf /"}))


def test_a_reply_that_is_not_json_is_a_proposal_error():
    with pytest.raises(ProposalError, match="not valid JSON"):
        parse_manifest("here is your manifest, boss")


def _llm_task() -> AgentTask:
    return AgentTask(
        task_id="task-llm",
        event_id=manifests.COOLING["event_id"],
        device_id=DEVICE,
        trigger_type=TriggerType.CONTEXT_TRIGGER,
        event="HIGH_HEAT_DETECTED",
        raw_payload={"data": {"temp_c": 85.4, "threshold": 80.0}},
    )


# --- the stub needs no key ---------------------------------------------------


def test_the_stub_agent_needs_no_api_key_and_no_network(registry, schema, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    proposal = StubManifestAgent(registry).propose(_llm_task(), schema, "a" * 16)
    assert proposal.ok
    assert proposal.prompt_tokens == 0 and proposal.completion_tokens == 0


def test_the_stub_agent_is_deterministic(registry, schema):
    first = StubManifestAgent(registry).propose(_llm_task(), schema, "a" * 16).manifest
    second = StubManifestAgent(registry).propose(_llm_task(), schema, "a" * 16).manifest
    assert first.manifest_hash == second.manifest_hash

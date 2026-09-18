"""M18 — A/B slots, local leases, probation and offline rollback (RESEARCH.md §8/§10).

Every test in this file runs with no server in existence. That is the point: the
v0.1 device could not undo anything without the server process that scheduled
the undo, and these are the tests that would have failed then.
"""

import json
import logging
import time
from pathlib import Path

import pytest

import config
from edge_node import slots as slot_store
from edge_node.slots import DeviceState, Lease, SlotRecord, load_state, save_state
from edge_node.supervisor import SafetyState
from edge_node.virtual_device import VirtualDevice
from server.compiler.compiler import compile_manifest
from server.compiler.runtime import ControllerFault
from server.manifest.registry import load_registry
from server.ota import package as ota
from server.ota.package import Rejection
from server.schemas import load_hardware_schema
from server.sim import scenarios
from server.sim.world import ThermalWorld, WorldConfig
from tests.fixtures import manifests

DEVICE = "pi_node_alpha"
KEY = b"\x07" * 32
ISSUED_AT = 1770000000


@pytest.fixture(autouse=True)
def quiet():
    for name in ("caef.supervisor", "caef.device", "caef.slots", "caef.ota"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    yield
    for name in ("caef.supervisor", "caef.device", "caef.slots", "caef.ota"):
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture
def registry():
    return load_registry("1.0.0")


@pytest.fixture
def schema():
    return load_hardware_schema(DEVICE)


@pytest.fixture
def baseline(registry, schema):
    """The factory image: monitor, beat, raise the situation. No actuators."""
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
def morph(registry, schema, baseline):
    return compile_manifest(
        manifests.cooling(current_firmware_hash=baseline.artifact_hash), registry, schema
    ).program


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "device_state.json"


def build_device(world, schema, registry, state_path, clock=time.time):
    return VirtualDevice(
        device_id=DEVICE,
        world=world,
        schema=schema,
        registry=registry,
        state_path=state_path,
        wall_clock=clock,
        signing_key=KEY,
    )


@pytest.fixture
def device(schema, registry, state_path, baseline):
    world = ThermalWorld(scenarios.get("gradual_overheat").world)
    node = build_device(world, schema, registry, state_path)
    node.provision(baseline)
    return node


def package_for(program, sequence=1, lease=40):
    return ota.build_package(
        program, sequence_number=sequence, issued_at=ISSUED_AT,
        lease_duration_seconds=lease, key=KEY,
    )


def run(device, ticks):
    return [device.tick() for _ in range(ticks)]


def events(reports):
    return [event for report in reports for event in report.events]


# --- persistence -------------------------------------------------------------


def test_state_survives_a_round_trip(state_path):
    state = slot_store.blank_state(DEVICE)
    state.slots["A"].lease = Lease(duration_seconds=300, elapsed_seconds=12.0,
                                   installed_at_wall=1000.0)
    state.last_accepted_sequence = 9
    save_state(state, state_path)

    restored = load_state(state_path, DEVICE)
    assert restored.last_accepted_sequence == 9
    assert restored.slots["A"].lease.elapsed_seconds == 12.0


def test_a_write_leaves_no_partial_file_behind(state_path):
    """Atomic replace: the directory must not accumulate temp files, and the
    target must always be parseable."""
    state = slot_store.blank_state(DEVICE)
    for index in range(5):
        state.last_accepted_sequence = index
        save_state(state, state_path)
        json.loads(state_path.read_text())
    leftovers = [path for path in state_path.parent.iterdir() if path.name != state_path.name]
    assert leftovers == []


def test_unreadable_state_does_not_stop_the_device_booting(state_path, schema, registry):
    """A device that refuses to boot has no supervisor at all, which is worse
    than one that has forgotten its slots."""
    state_path.write_text("{ this is not json")
    node = build_device(ThermalWorld(WorldConfig()), schema, registry, state_path)
    assert node.state.device_id == DEVICE
    assert node.state.running().empty


def test_state_belonging_to_another_device_is_not_adopted(state_path, schema, registry):
    save_state(slot_store.blank_state("pi_node_beta"), state_path)
    node = build_device(ThermalWorld(WorldConfig()), schema, registry, state_path)
    assert node.state.device_id == DEVICE


# --- installation and probation ---------------------------------------------


def test_a_verified_package_lands_in_the_inactive_slot(device, morph):
    outcome = device.install(package_for(morph))
    assert outcome.accepted is True
    assert outcome.slot == "B"
    assert device.state.active_slot == "A", "the active slot is untouched during probation"
    assert device.state.candidate_status == "probation"
    assert device.state.running_slot == "B", "but the candidate is what is running"


def test_a_candidate_is_activated_only_after_probation(device, morph):
    device.install(package_for(morph))
    reports = run(device, config.PROBATION_HEALTHY_TICKS)
    assert "candidate_activated" in events(reports)
    assert device.state.active_slot == "B"
    assert device.state.last_known_good_slot == "A"
    assert device.state.candidate_status == "active"


def test_the_previous_slot_is_kept_as_last_known_good(device, morph, baseline):
    device.install(package_for(morph))
    run(device, config.PROBATION_HEALTHY_TICKS)
    assert device.state.last_known_good().artifact_hash == baseline.artifact_hash


def test_a_rejected_package_changes_nothing(device, morph, baseline):
    """A package for another device must not disturb the slots at all."""
    stranger = compile_manifest(
        manifests.cooling(device_id="pi_node_beta", current_firmware_hash=baseline.artifact_hash),
        load_registry("1.0.0"),
        load_hardware_schema(DEVICE),
    ).program
    outcome = device.install(package_for(stranger))
    assert outcome.accepted is False
    assert outcome.verdict.rejection is Rejection.WRONG_DEVICE
    assert device.state.slots["B"].empty
    assert device.state.running_slot == "A"


def test_the_replay_watermark_survives_a_restart(device, morph):
    """The realistic replay: capture a morph package, wait for it to expire, and
    re-send it once the device is back on the firmware it was built against.

    The base-firmware check no longer helps at that point — the base hash
    matches again — so the persisted sequence watermark is the only thing left,
    and a restart in between must not clear it.
    """
    package = package_for(morph, sequence=7, lease=20)
    assert device.install(package).accepted is True
    run(device, 40)
    assert device.state.running_slot == "A", "the morph must have expired by now"

    restarted = device.restart()
    assert restarted.state.last_accepted_sequence == 7

    replay = restarted.install(package)
    assert replay.accepted is False
    assert replay.verdict.rejection is Rejection.REPLAYED_SEQUENCE


def test_a_failed_candidate_still_moves_the_watermark(device, morph, monkeypatch):
    """Acceptance moves the watermark, not activation: a package that was
    accepted and then failed probation must not be replayable either."""
    device.install(package_for(morph, sequence=4))

    def explode(_observation):
        raise ControllerFault("injected fault")

    monkeypatch.setattr(device.controller, "step", explode)
    device.tick()

    assert device.state.candidate_status == "failed"
    assert device.state.last_accepted_sequence == 4
    assert device.install(package_for(morph, sequence=4)).verdict.rejection is (
        Rejection.REPLAYED_SEQUENCE
    )


def test_a_package_built_against_stale_firmware_is_refused(device, morph, registry, schema):
    """The device is running the baseline; a package built against something
    else was reasoned about from a state that no longer exists."""
    stale = compile_manifest(
        manifests.cooling(current_firmware_hash="dead" * 4), registry, schema
    ).program
    outcome = device.install(package_for(stale))
    assert outcome.verdict.rejection is Rejection.STALE_BASE_FIRMWARE


# --- leases ------------------------------------------------------------------


def test_a_morph_reverts_when_its_lease_expires_with_no_server(device, morph, baseline):
    """The headline property. Nothing in this test can reach a server, because
    there is no server — the lease is the device's own.
    """
    device.install(package_for(morph, lease=30))
    reports = run(device, 60)
    assert "lease_expired" in events(reports)
    assert device.state.running_slot == "A"
    assert device.state.running().artifact_hash == baseline.artifact_hash


def test_a_restart_does_not_erase_a_pending_lease(device, morph):
    device.install(package_for(morph, lease=60))
    run(device, 20)
    before = device.state.running().lease.remaining(time.time())
    assert 0 < before < 60

    restarted = device.restart()
    after = restarted.state.running().lease.remaining(time.time())
    assert after == pytest.approx(before, abs=1.0)

    reports = run(restarted, 60)
    assert "lease_expired" in events(reports)
    assert restarted.state.running_slot == "A"


def test_a_restart_cannot_be_used_to_renew_a_lease(device, morph):
    """Restarting repeatedly must not keep resetting the clock — otherwise a
    crash loop becomes an unbounded morph."""
    node = device
    node.install(package_for(morph, lease=40))
    for _ in range(6):
        run(node, 5)
        node = node.restart()
    remaining = node.state.running().lease
    assert remaining is None or remaining.remaining(time.time()) < 40


def test_a_stopped_clock_cannot_extend_a_lease():
    """Expiry uses whichever clock advanced more, so freezing one does nothing."""
    lease = Lease(duration_seconds=10, installed_at_wall=1000.0)
    lease.advance(11.0)
    assert lease.expired(now_wall=1000.0) is True  # wall time frozen


def test_a_rewound_clock_cannot_extend_a_lease():
    lease = Lease(duration_seconds=10, elapsed_seconds=0.0, installed_at_wall=1000.0)
    assert lease.expired(now_wall=1011.0) is True
    assert lease.expired(now_wall=900.0) is False  # rewound: falls back to elapsed
    lease.advance(10.0)
    assert lease.expired(now_wall=900.0) is True


def test_a_durable_install_carries_no_lease_and_does_not_revert(device, morph):
    """An auto-patch is meant to persist (LOOPS.md §4.5)."""
    device.install(package_for(morph, lease=None))
    reports = run(device, 60)
    assert "lease_expired" not in events(reports)
    assert device.state.running_slot == "B"


# --- crash, safe state, local rollback ---------------------------------------


def test_a_crash_during_probation_reverts_locally(device, morph, baseline, monkeypatch):
    device.install(package_for(morph))

    def explode(_observation):
        raise ControllerFault("injected fault")

    monkeypatch.setattr(device.controller, "step", explode)
    report = device.tick()

    assert "controller_fault" in report.events
    assert "probation_failed" in report.events and "reverted" in report.events
    assert device.state.running_slot == "A"
    assert device.state.running().artifact_hash == baseline.artifact_hash
    assert device.state.candidate_status == "failed"


def test_a_crash_leaves_the_device_cooling_before_it_reverts(device, morph, monkeypatch):
    """Safe state is entered while the device is hot, and the revert restores a
    known-good artifact. Neither step consults a server."""
    run(device, 28)  # let the device get genuinely hot first
    device.install(package_for(morph))

    def explode(_observation):
        raise ControllerFault("injected fault")

    monkeypatch.setattr(device.controller, "step", explode)
    report = device.tick()
    assert report.sensor_temp_c > config.SAFE_STATE_COOLING_TEMP_C
    assert report.fan_state == "on"
    assert device.state.running_slot == "A"


def test_an_established_artifact_gets_the_local_crash_budget(device, morph, monkeypatch):
    """Past probation, a single fault is not a rollback: the device spends its
    configured local failure budget first."""
    device.install(package_for(morph, lease=None))
    run(device, config.PROBATION_HEALTHY_TICKS)
    assert device.state.candidate_status == "active"

    faults = 0

    def sometimes_explode(_observation):
        nonlocal faults
        faults += 1
        raise ControllerFault(f"injected fault {faults}")

    monkeypatch.setattr(device.controller, "step", sometimes_explode)
    device.tick()
    assert device.state.running_slot == "B", "one fault is not a rollback"
    assert device.state.failure_count == 1


def test_a_device_with_nothing_to_fall_back_to_holds_in_safe_state(
    schema, registry, state_path, morph
):
    """No known-good artifact is the operator-escalation case. The supervisor's
    safe state is then the whole of the device's safety, which is why it keeps
    cooling rather than merely stopping."""
    world = ThermalWorld(scenarios.get("gradual_overheat").world)
    node = build_device(world, schema, registry, state_path)
    node.state.slots["A"] = SlotRecord(slot="A")  # nothing installed
    assert node.revert_local("nothing to restore") is False
    assert node.supervisor.state is SafetyState.SAFE_STATE


def test_the_fallback_runs_on_the_way_out(device, morph):
    """A morph that promised to leave cooling on gets to keep that promise."""
    device.install(package_for(morph, lease=20))
    before = len(device.supervisor.decisions)
    run(device, 40)
    after = [
        decision
        for decision in device.supervisor.decisions[before:]
        if decision.intent.capability == "enter_safe_idle"
    ]
    assert after, "the declared fallback must actually execute on reversion"


# --- the whole loop ----------------------------------------------------------


def test_morph_install_probation_activation_lease_revert(device, morph, baseline):
    """The sequence the demo prints, asserted end to end."""
    run(device, 28)
    assert device.world.device_temp_c > 70.0

    outcome = device.install(package_for(morph, lease=30))
    assert outcome.accepted

    reports = run(device, 8)
    assert "candidate_activated" in events(reports)
    cooled = min(report.device_temp_c for report in reports)
    assert cooled < device.world.config.initial_device_c + 30

    reports = run(device, 60)
    assert "lease_expired" in events(reports)
    assert device.state.running().manifest_id == baseline.manifest_id
    assert device.state.last_accepted_sequence == 1

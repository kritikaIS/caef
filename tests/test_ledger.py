"""M19 — deployment accounting (RESEARCH.md §11).

The bug this exists to prevent is a dashboard that says "deployed" about a
device nobody has heard from. `deployer.deploy` in the v0.1 path writes a
`deployed` history row even when the OTA push finds nobody home; these states
keep "we tried to send it", "the device took it" and "the device is running it"
apart, and the transition graph makes skipping between them impossible rather
than merely discouraged.
"""

import pytest

from server.db.models import Device, SessionLocal
from server.deploy import ledger
from server.deploy.ledger import IllegalTransition, State
from server.schemas import AdaptationMode

DEVICE = "pi_node_alpha"


@pytest.fixture(autouse=True)
def device_row():
    with SessionLocal() as db:
        db.add(Device(id=DEVICE, mcu_type="RaspberryPi_4B"))
        db.commit()


def open_one(**overrides) -> str:
    return ledger.open_deployment(
        device_id=DEVICE,
        mode=AdaptationMode.MANIFEST_COMPILER,
        manifest_id="manifest-cooling-1",
        manifest_hash="a" * 64,
        device_event_time=171542000,
        **overrides,
    )


def walk(deployment_id: str, *states, **kwargs) -> None:
    for state in states:
        ledger.advance(deployment_id, state, **kwargs)


HAPPY_PATH = (
    State.MANIFEST_VALIDATED,
    State.COMPILED,
    State.SIMULATION_VERIFIED,
    State.SIGNED,
    State.DELIVERY_ATTEMPTED,
    State.ACCEPTED_BY_DEVICE,
    State.ACTIVE_ON_DEVICE,
)


# --- the graph ---------------------------------------------------------------


def test_a_deployment_starts_as_a_proposal():
    deployment_id = open_one()
    assert ledger.state_of(deployment_id) is State.PROPOSED
    assert [t.to_state for t in ledger.transitions(deployment_id)] == [State.PROPOSED]


def test_the_full_lifecycle_is_walkable():
    deployment_id = open_one()
    walk(deployment_id, *HAPPY_PATH)
    assert ledger.state_of(deployment_id) is State.ACTIVE_ON_DEVICE
    assert [t.to_state for t in ledger.transitions(deployment_id)] == [
        State.PROPOSED,
        *HAPPY_PATH,
    ]


def test_a_send_is_not_an_arrival():
    """The headline rule: firmware is never recorded active because an OTA send
    was attempted. Something has to come back from the device."""
    deployment_id = open_one()
    walk(deployment_id, *HAPPY_PATH[:5])
    assert ledger.state_of(deployment_id) is State.DELIVERY_ATTEMPTED

    with pytest.raises(IllegalTransition, match="a send is not an arrival"):
        ledger.advance(deployment_id, State.ACTIVE_ON_DEVICE)
    assert ledger.state_of(deployment_id) is State.DELIVERY_ATTEMPTED


def test_acceptance_is_not_activation():
    """Probation can still fail and a lease can expire before promotion."""
    deployment_id = open_one()
    walk(deployment_id, *HAPPY_PATH[:6])
    assert ledger.state_of(deployment_id) is State.ACCEPTED_BY_DEVICE
    ledger.advance(deployment_id, State.REVERTED, detail="probation failed")
    assert ledger.state_of(deployment_id) is State.REVERTED


def test_stages_cannot_be_skipped():
    deployment_id = open_one()
    with pytest.raises(IllegalTransition):
        ledger.advance(deployment_id, State.SIGNED)
    with pytest.raises(IllegalTransition):
        ledger.advance(deployment_id, State.ACTIVE_ON_DEVICE)
    assert ledger.state_of(deployment_id) is State.PROPOSED


def test_a_failed_verification_can_only_be_rejected():
    """The gate the whole pipeline hangs on: nothing gets signed after a failed
    simulation."""
    deployment_id = open_one()
    walk(deployment_id, State.MANIFEST_VALIDATED, State.COMPILED)
    ledger.advance(deployment_id, State.REJECTED, detail="verification failed")
    for forward in (State.SIMULATION_VERIFIED, State.SIGNED, State.ACTIVE_ON_DEVICE):
        with pytest.raises(IllegalTransition):
            ledger.advance(deployment_id, forward)


def test_terminal_states_are_terminal():
    """Each terminal is reached from where it actually makes sense — `rejected`
    before the device ever had it, `reverted`/`rolled_back` after — and nothing
    follows any of them."""
    for prefix, terminal in (
        ((), State.REJECTED),
        (HAPPY_PATH, State.REVERTED),
        (HAPPY_PATH, State.ROLLED_BACK),
    ):
        deployment_id = open_one()
        walk(deployment_id, *prefix)
        ledger.advance(deployment_id, terminal)
        assert ledger.state_of(deployment_id) is terminal
        for forward in (State.ACTIVE_ON_DEVICE, State.SIGNED, State.PROPOSED):
            with pytest.raises(IllegalTransition):
                ledger.advance(deployment_id, forward)


def test_an_active_deployment_cannot_be_retconned_as_rejected():
    """Once the device says it is running something, the honest endings are
    "it was reverted" and "it was rolled back" — not "we rejected it"."""
    deployment_id = open_one()
    walk(deployment_id, *HAPPY_PATH)
    with pytest.raises(IllegalTransition):
        ledger.advance(deployment_id, State.REJECTED)


def test_every_state_appears_in_the_graph():
    from server.schemas import DeploymentState

    assert set(ledger.TRANSITIONS) == set(DeploymentState)


# --- clocks ------------------------------------------------------------------


def test_both_clocks_are_recorded():
    deployment_id = open_one()
    ledger.advance(
        deployment_id, State.MANIFEST_VALIDATED, device_event_time=171542099
    )
    deployment = ledger.get(deployment_id)
    assert deployment.server_received_at is not None
    assert deployment.device_event_time == 171542099


def test_ordering_does_not_depend_on_any_timestamp():
    """Transitions are ordered by an autoincrement id. Two of them can share a
    server timestamp, and the device's clock is not ours to trust at all."""
    deployment_id = open_one()
    walk(deployment_id, *HAPPY_PATH, device_event_time=1)
    rows = ledger.transitions(deployment_id)
    assert [row.id for row in rows] == sorted(row.id for row in rows)
    assert [row.to_state for row in rows][-1] is State.ACTIVE_ON_DEVICE
    # Even with every device timestamp identical — or moving backwards — the
    # recorded order is unchanged.
    assert len({row.device_event_time for row in rows[1:]}) == 1


# --- queries -----------------------------------------------------------------


def test_active_on_device_reports_only_what_is_really_running():
    attempted = open_one()
    walk(attempted, *HAPPY_PATH[:5])
    assert ledger.active_on_device(DEVICE) is None, "a send is not a running device"

    live = open_one()
    walk(live, *HAPPY_PATH)
    assert ledger.active_on_device(DEVICE).id == live


def test_counts_by_state_summarise_the_ledger():
    rejected = open_one()
    ledger.advance(rejected, State.REJECTED, detail="unknown capability")
    live = open_one()
    walk(live, *HAPPY_PATH)

    tally = ledger.counts_by_state(DEVICE)
    assert tally == {"rejected": 1, "active_on_device": 1}


def test_both_adaptation_modes_share_one_ledger():
    """So the two arms can be compared from one table instead of two divergent
    ones (RESEARCH.md §1)."""
    baseline = ledger.open_deployment(
        device_id=DEVICE, mode=AdaptationMode.SOURCE_GENERATION, patch_id=None
    )
    contract = open_one()
    modes = {ledger.get(baseline).mode, ledger.get(contract).mode}
    assert modes == {AdaptationMode.SOURCE_GENERATION, AdaptationMode.MANIFEST_COMPILER}


def test_facts_are_recorded_as_they_become_known():
    deployment_id = open_one()
    ledger.advance(deployment_id, State.MANIFEST_VALIDATED)
    ledger.advance(deployment_id, State.COMPILED, artifact_hash="b" * 64)
    ledger.advance(deployment_id, State.SIMULATION_VERIFIED)
    ledger.advance(deployment_id, State.SIGNED, sequence_number=4, lease_duration_seconds=300)

    deployment = ledger.get(deployment_id)
    assert deployment.artifact_hash == "b" * 64
    assert deployment.sequence_number == 4
    assert deployment.lease_duration_seconds == 300

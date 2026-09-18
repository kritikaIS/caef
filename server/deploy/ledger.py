"""Deployment accounting — what actually happened to an artifact.

RESEARCH.md §11. The v0.1 ledger recorded a `deployed` row the moment
`deployer.deploy` had *tried* to push, whether or not anyone answered. An
operator reading "deployed" believes the device is running it; a dashboard built
on that belief is wrong exactly when it matters, during a partition.

So the states are separated and the transitions between them are a fixed graph.
The one that matters most is that `delivery_attempted` cannot reach
`active_on_device` directly: something has to come back from the device first.
An illegal transition raises rather than being silently coerced — a ledger that
quietly accepts an impossible history is not an audit trail.

Nothing here calls a model. It is bookkeeping over the database, and it is on
the path the Safety Rollback Protocol reads (SAFETY_PROTOCOL.md §5).
"""

import logging
from datetime import datetime

from server.db.models import Deployment, DeploymentTransition, SessionLocal, utcnow
from server.schemas import AdaptationMode, DeploymentState

log = logging.getLogger("caef.ledger")

State = DeploymentState

# Terminal states. Nothing follows them; a new attempt is a new deployment.
TERMINAL = frozenset({State.REJECTED, State.REVERTED, State.ROLLED_BACK})

# The legal graph. Read it as "what can honestly be said next".
TRANSITIONS: dict[DeploymentState, frozenset[DeploymentState]] = {
    State.PROPOSED: frozenset({State.MANIFEST_VALIDATED, State.REJECTED}),
    State.MANIFEST_VALIDATED: frozenset({State.COMPILED, State.REJECTED}),
    State.COMPILED: frozenset({State.SIMULATION_VERIFIED, State.REJECTED}),
    State.SIMULATION_VERIFIED: frozenset({State.SIGNED, State.REJECTED}),
    State.SIGNED: frozenset({State.DELIVERY_ATTEMPTED, State.REJECTED}),
    # A send is not an arrival. The only way forward is the device saying so.
    State.DELIVERY_ATTEMPTED: frozenset({State.ACCEPTED_BY_DEVICE, State.REJECTED}),
    # Accepted is not running: probation can still fail, and a lease can expire
    # before promotion (RESEARCH.md §10).
    State.ACCEPTED_BY_DEVICE: frozenset(
        {State.ACTIVE_ON_DEVICE, State.REJECTED, State.REVERTED, State.ROLLED_BACK}
    ),
    State.ACTIVE_ON_DEVICE: frozenset({State.REVERTED, State.ROLLED_BACK}),
    State.REJECTED: frozenset(),
    State.REVERTED: frozenset(),
    State.ROLLED_BACK: frozenset(),
}


class IllegalTransition(ValueError):
    """A state change the deployment lifecycle does not permit."""


def open_deployment(
    device_id: str,
    mode: AdaptationMode,
    event_id: str | None = None,
    manifest_id: str | None = None,
    manifest_hash: str | None = None,
    artifact_hash: str | None = None,
    capability_registry_version: str | None = None,
    base_firmware_hash: str | None = None,
    device_event_time: int | None = None,
    patch_id: str | None = None,
    detail: str | None = None,
) -> str:
    """Start a deployment in `proposed`. Returns its id."""
    with SessionLocal() as db:
        deployment = Deployment(
            device_id=device_id,
            event_id=event_id,
            mode=mode,
            state=State.PROPOSED,
            manifest_id=manifest_id,
            manifest_hash=manifest_hash,
            artifact_hash=artifact_hash,
            capability_registry_version=capability_registry_version,
            base_firmware_hash=base_firmware_hash,
            patch_id=patch_id,
            device_event_time=device_event_time,
        )
        db.add(deployment)
        db.flush()
        db.add(
            DeploymentTransition(
                deployment_id=deployment.id,
                from_state=None,
                to_state=State.PROPOSED,
                device_event_time=device_event_time,
                detail=detail,
            )
        )
        db.commit()
        return deployment.id


def advance(
    deployment_id: str,
    to_state: DeploymentState,
    detail: str | None = None,
    device_event_time: int | None = None,
    **fields,
) -> DeploymentState:
    """Move a deployment forward and append the transition.

    `fields` carries whatever became known at this step — the artifact hash once
    it is compiled, the sequence number once it is signed — so the row grows as
    the facts do rather than being back-filled later.
    """
    with SessionLocal() as db:
        deployment = db.get(Deployment, deployment_id)
        if deployment is None:
            raise IllegalTransition(f"no deployment {deployment_id}")

        current = deployment.state
        if to_state not in TRANSITIONS[current]:
            raise IllegalTransition(
                f"{current.value} -> {to_state.value} is not a legal transition"
                + (
                    " (a send is not an arrival: the device has to answer first)"
                    if current is State.DELIVERY_ATTEMPTED
                    and to_state is State.ACTIVE_ON_DEVICE
                    else ""
                )
            )

        deployment.state = to_state
        deployment.updated_at = utcnow()
        if detail:
            deployment.reason = detail
        if device_event_time is not None:
            deployment.device_event_time = device_event_time
        for name, value in fields.items():
            setattr(deployment, name, value)

        db.add(
            DeploymentTransition(
                deployment_id=deployment_id,
                from_state=current,
                to_state=to_state,
                device_event_time=device_event_time,
                detail=detail,
            )
        )
        db.commit()

    log.info("deployment %s: %s -> %s%s", deployment_id, current.value, to_state.value,
             f" ({detail})" if detail else "")
    return to_state


def get(deployment_id: str) -> Deployment | None:
    with SessionLocal() as db:
        return db.get(Deployment, deployment_id)


def state_of(deployment_id: str) -> DeploymentState | None:
    deployment = get(deployment_id)
    return deployment.state if deployment else None


def transitions(deployment_id: str) -> list[DeploymentTransition]:
    """In insertion order — by autoincrement id, never by timestamp."""
    with SessionLocal() as db:
        return (
            db.query(DeploymentTransition)
            .filter(DeploymentTransition.deployment_id == deployment_id)
            .order_by(DeploymentTransition.id)
            .all()
        )


def active_on_device(device_id: str) -> Deployment | None:
    """What the server believes is actually *running*, on the device's own say-so.

    The only query an operator dashboard should use for "what is this device
    running" — anything earlier in the graph is an intention, not a fact.
    """
    with SessionLocal() as db:
        return (
            db.query(Deployment)
            .filter(
                Deployment.device_id == device_id,
                Deployment.state == State.ACTIVE_ON_DEVICE,
            )
            .order_by(Deployment.updated_at.desc())
            .first()
        )


def deployments_for(device_id: str, limit: int = 50) -> list[Deployment]:
    with SessionLocal() as db:
        return (
            db.query(Deployment)
            .filter(Deployment.device_id == device_id)
            .order_by(Deployment.server_received_at.desc())
            .limit(limit)
            .all()
        )


def counts_by_state(device_id: str | None = None) -> dict[str, int]:
    """Summary for the dashboard and the experiment harness."""
    with SessionLocal() as db:
        query = db.query(Deployment)
        if device_id:
            query = query.filter(Deployment.device_id == device_id)
        tally: dict[str, int] = {}
        for deployment in query.all():
            tally[deployment.state.value] = tally.get(deployment.state.value, 0) + 1
        return tally


def next_sequence(device_id: str) -> int:
    """The next OTA sequence number for a device.

    Taken from the highest number this server has ever *issued* to the device,
    not from the device's own watermark: a package the device rejected still
    consumed a number, and reusing it would look like a replay from the outside
    (RESEARCH.md §9).
    """
    with SessionLocal() as db:
        highest = (
            db.query(Deployment.sequence_number)
            .filter(
                Deployment.device_id == device_id,
                Deployment.sequence_number.isnot(None),
            )
            .order_by(Deployment.sequence_number.desc())
            .first()
        )
    return (highest[0] if highest else 0) + 1


def live_for_event(device_id: str, event_name: str) -> Deployment | None:
    """A deployment for this same situation that the device already has.

    The manifest-mode counterpart of the baseline's duplicate-morph guard
    (LOOPS.md §2a): a device that keeps re-reporting a situation it is already
    adapting to must not collect a second morph — and re-issuing would also
    restart the lease, so a situation outlasting one window would never expire.
    """
    from server.db.models import Event

    with SessionLocal() as db:
        return (
            db.query(Deployment)
            .join(Event, Deployment.event_id == Event.id)
            .filter(
                Deployment.device_id == device_id,
                Event.event == event_name,
                Deployment.state.in_(
                    [State.ACCEPTED_BY_DEVICE, State.ACTIVE_ON_DEVICE]
                ),
            )
            .order_by(Deployment.updated_at.desc())
            .first()
        )

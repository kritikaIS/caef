"""SQLAlchemy models: Device, Event, Patch, HistoryRecord (TDD.md §2.8).

The History Table is append-only (TRD.md §5) and FKEY-linked back to device,
event and patch so any deployed artifact is traceable end-to-end (NFR-5).
Engine is driven by config.DATABASE_URL — Supabase Postgres in deployment,
SQLite for offline tests, no code difference.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

import config
from server.schemas import (
    AdaptationMode,
    DeploymentState,
    RecordStatus,
    RecordType,
    TriggerType,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. pi_node_alpha
    mcu_type: Mapped[str] = mapped_column(String)
    # A/B partition model (SAFETY_PROTOCOL.md §6): the previous active slot is
    # kept as inactive so "last known-good" is always immediately flashable.
    active_fw_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    inactive_fw_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    assigned_fw_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    last_poll_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Per-device strike counter (SAFETY_PROTOCOL.md §5). generation_halted stays
    # True until an operator acknowledges — never auto-resumes.
    strike_count: Mapped[int] = mapped_column(Integer, default=0)
    generation_halted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    events: Mapped[list["Event"]] = relationship(back_populates="device")
    history: Mapped[list["HistoryRecord"]] = relationship(back_populates="device")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    trigger_type: Mapped[TriggerType] = mapped_column(Enum(TriggerType))
    event: Mapped[str] = mapped_column(String)  # e.g. HIGH_HEAT_DETECTED
    # Device is the clock (ARCHITECTURE.md §4.2); received_at is server-side only
    # for latency metrics.
    timestamp: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    current_state_hash: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    # Shared Guard Rail + Sandbox retry budget, per event (SAFETY_PROTOCOL.md §4).
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)

    device: Mapped[Device] = relationship(back_populates="events")
    patches: Mapped[list["Patch"]] = relationship(back_populates="event")


class Patch(Base):
    __tablename__ = "patches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    # Stored verbatim for audit (NFR-5): the plan, the code, and the tool-call
    # trace Guard Rail used to prove pin provenance.
    plan: Mapped[str] = mapped_column(Text)
    target_file: Mapped[str] = mapped_column(String)
    code: Mapped[str] = mapped_column(Text)
    pins_referenced: Mapped[list] = mapped_column(JSON, default=list)
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    fw_hash: Mapped[str] = mapped_column(String, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    guardrail_status: Mapped[str | None] = mapped_column(String, nullable=True)
    guardrail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    sandbox_status: Mapped[str | None] = mapped_column(String, nullable=True)
    sandbox_logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    sandbox_runtime_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    event: Mapped[Event] = relationship(back_populates="patches")


class HistoryRecord(Base):
    """Append-only ledger: Time | Poll id | Event id | Patch id | FW hash."""

    __tablename__ = "history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    time: Mapped[int] = mapped_column(Integer)  # device-authoritative event time
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    poll_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    # Nullable for reversion/rollback rows that reference a prior patch's artifact
    # rather than introducing new code (DATA_SCHEMAS.md §7).
    patch_id: Mapped[str | None] = mapped_column(ForeignKey("patches.id"), nullable=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    fw_hash: Mapped[str] = mapped_column(String)
    record_type: Mapped[RecordType] = mapped_column(Enum(RecordType))
    status: Mapped[RecordStatus] = mapped_column(Enum(RecordStatus))

    device: Mapped[Device] = relationship(back_populates="history")



class Deployment(Base):
    """One artifact's journey from proposal to whatever became of it.

    Sits alongside the v0.1 `HistoryRecord` rather than replacing it: the
    baseline's ledger keeps its shape and its tests, and this adds the states
    that ledger cannot express (RESEARCH.md §11).

    Two clocks, deliberately. `server_received_at` and every transition's `at`
    are server-side; `device_event_time` is the device's own clock, recorded
    because the device is the authority on when a thing happened to it
    (ARCHITECTURE.md §4.2) — but never used for ordering, because an untrusted
    device timestamp is not an ordering primitive. Ordering comes from the
    transitions' autoincrement id.
    """

    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    mode: Mapped[AdaptationMode] = mapped_column(Enum(AdaptationMode))
    state: Mapped[DeploymentState] = mapped_column(Enum(DeploymentState), index=True)

    # Manifest-mode identifiers. Null in source_generation mode, where the
    # artifact is a Patch row instead.
    manifest_id: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact_hash: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    capability_registry_version: Mapped[str | None] = mapped_column(String, nullable=True)
    base_firmware_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    sequence_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Source-generation identifier, so both arms land in one table.
    patch_id: Mapped[str | None] = mapped_column(ForeignKey("patches.id"), nullable=True)

    server_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    device_event_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    transitions: Mapped[list["DeploymentTransition"]] = relationship(
        back_populates="deployment", order_by="DeploymentTransition.id"
    )


class DeploymentTransition(Base):
    """One state change, append-only.

    The primary key is a plain autoincrement integer and it is what defines
    order. Not `at`, and certainly not the device's clock: two transitions can
    share a timestamp, clocks can move backwards, and a device's clock is not
    ours to trust (RESEARCH.md §11).
    """

    __tablename__ = "deployment_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), index=True)
    from_state: Mapped[DeploymentState | None] = mapped_column(
        Enum(DeploymentState), nullable=True
    )
    to_state: Mapped[DeploymentState] = mapped_column(Enum(DeploymentState))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    device_event_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    deployment: Mapped[Deployment] = relationship(back_populates="transitions")


def aware(value: datetime | None) -> datetime | None:
    """Coerce a stored timestamp back to UTC-aware.

    SQLite has no timestamp type, so SQLAlchemy hands back naive datetimes even
    for `DateTime(timezone=True)` columns. Postgres does not. Every age
    comparison against a stored time goes through here so the two backends
    behave identically (CRASH_ATTRIBUTION_WINDOW_SECONDS is safety-relevant).
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


_engine_kwargs = {}
if config.DATABASE_URL.startswith("sqlite"):
    if config.DATABASE_URL.endswith(":memory:"):
        # `:memory:` needs a StaticPool to stay visible across threads, and a
        # StaticPool shares one connection — so a reader closing its session
        # ROLLBACKs a writer thread's uncommitted INSERT. Deploys write from
        # worker threads (asyncio.to_thread), so that race silently loses
        # History rows. Refuse it; a file-backed SQLite DB has neither problem.
        raise ValueError(
            "DATABASE_URL=sqlite:///:memory: is unsafe: deploys write from worker "
            "threads and a shared connection loses those writes. Use a file path."
        )
    # Deploys and reversions run off the event loop, so connections cross threads.
    _engine_kwargs = {"connect_args": {"check_same_thread": False}}

engine = create_engine(config.DATABASE_URL, future=True, **_engine_kwargs)
SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)

if engine.dialect.name == "sqlite":
    # SQLite ignores foreign keys unless asked, per connection. Postgres always
    # enforces them, so without this an orphaned History row — the exact thing
    # FKEY traceability forbids (NFR-5) — passes on SQLite and only fails in
    # deployment.
    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_db() -> None:
    Base.metadata.create_all(engine)

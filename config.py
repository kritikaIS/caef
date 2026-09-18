"""Single source of tunables for the whole CAEF pipeline (TDD.md §6).

Any safety-relevant number (retry cap, sandbox timeout, reversion window,
forbidden pins, denylist) MUST be read from here, never inlined at the call
site. CLAUDE.md §4 treats an inline safety constant as a build failure.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


# --- Adaptation mode ---------------------------------------------------------
# Which adaptation pipeline a Task is handled by (RESEARCH.md §1):
#
#   source_generation  the v0.1 baseline. The Agent emits a complete Python
#                      firmware file; Guard Rail + Sandbox vet it. Preserved
#                      verbatim as the experimental control — its behaviour is
#                      unchanged by anything in the manifest pipeline.
#   manifest_compiler  the contract-constrained mode. The Agent emits a
#                      declarative Behavior Manifest; deterministic components
#                      validate, compile, simulate and sign it, and an immutable
#                      local supervisor owns the actuators.
#
# `source_generation` stays the default so an existing checkout behaves exactly
# as it did before this mode existed. `manifest_compiler` is the *recommended*
# mode and is what the demo, the compose stack and .env.example select.
SOURCE_GENERATION = "source_generation"
MANIFEST_COMPILER = "manifest_compiler"
ADAPTATION_MODES = (SOURCE_GENERATION, MANIFEST_COMPILER)
# An empty value means "not configured" — a `.env` line left blank must fall
# back to the default, not fail the process.
ADAPTATION_MODE = os.getenv("ADAPTATION_MODE", "").strip() or SOURCE_GENERATION
if ADAPTATION_MODE not in ADAPTATION_MODES:
    raise ValueError(
        f"ADAPTATION_MODE={ADAPTATION_MODE!r} is not one of {ADAPTATION_MODES}. "
        "An unrecognised mode must fail at import rather than silently fall back "
        "to a pipeline the operator did not ask for."
    )


# --- Storage -----------------------------------------------------------------
# Structured store: Supabase Postgres in deployment, SQLite for offline tests.
# SQLAlchemy either way, so this is a connection-string swap (TDD.md §2.8).
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'caef.db'}")

# RAG store: local SQLite FTS5 index, separate file from the structured store
# (TRD.md §5 permits one engine for both; we keep RAG local so E2E runs offline).
RAG_DB_PATH = Path(os.getenv("RAG_DB_PATH", ROOT / "caef_rag.db"))
# How many retrieved documents of each type reach the prompt (TDD.md §2.4).
RAG_DRIVER_DOC_LIMIT = _int("RAG_DRIVER_DOC_LIMIT", 5)
RAG_HISTORY_DOC_LIMIT = _int("RAG_HISTORY_DOC_LIMIT", 3)

HARDWARE_SCHEMA_DIR = Path(os.getenv("HARDWARE_SCHEMA_DIR", ROOT / "schemas"))
FIRMWARE_STORE_DIR = Path(os.getenv("FIRMWARE_STORE_DIR", ROOT / "firmware_store"))

# --- Safety ------------------------------------------------------------------
# Shared retry budget for Guard Rail + Sandbox failures, per event_id
# (SAFETY_PROTOCOL.md §4).
MAX_RETRIES = _int("MAX_RETRIES", 3)

# SAFETY_PROTOCOL.md §3.2
SANDBOX_TIMEOUT_SECONDS = _int("SANDBOX_TIMEOUT_SECONDS", 10)
SANDBOX_MEMORY_LIMIT = os.getenv("SANDBOX_MEMORY_LIMIT", "128m")
SANDBOX_CPU_LIMIT = os.getenv("SANDBOX_CPU_LIMIT", "0.5")
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "caef-sandbox:latest")
# The repo path as the *Docker daemon* sees it. Identical to ROOT when the
# runner is on the host; under docker-compose the runner is itself in a
# container talking to the host daemon, where `-v /app:...` would bind a path
# that does not exist on the host.
SANDBOX_HOST_REPO = Path(os.getenv("SANDBOX_HOST_REPO", ROOT))
# SAFETY_PROTOCOL.md §3.4: a candidate must emit this marker to count as healthy.
# The baseline firmware logs it on every tick; empty disables the check.
SANDBOX_HEALTHY_MARKER = os.getenv("SANDBOX_HEALTHY_MARKER", "[firmware]")
# Sandbox logs are stored on the Patch row for audit; cap so a chatty or looping
# candidate cannot bloat the DB.
SANDBOX_LOG_LIMIT_CHARS = _int("SANDBOX_LOG_LIMIT_CHARS", 20_000)

# SAFETY_PROTOCOL.md §5 — strikes are counted per device_id.
STRIKE_LIMIT = _int("STRIKE_LIMIT", 3)
# A device crash within this window of a deploy counts as a strike against it.
CRASH_ATTRIBUTION_WINDOW_SECONDS = _int("CRASH_ATTRIBUTION_WINDOW_SECONDS", 600)

# Situational Morphing reversion (LOOPS.md §2a, PRD OQ-1).
# v0.1 default is time-based; condition-based is the documented alternative.
REVERSION_WINDOW_SECONDS = _int("REVERSION_WINDOW_SECONDS", 300)
REVERSION_MODE = os.getenv("REVERSION_MODE", "time")  # time | condition | combined
REVERSION_RECOVERY_THRESHOLD_C = float(os.getenv("REVERSION_RECOVERY_THRESHOLD_C", 60.0))
# How often condition/combined mode re-checks the device's latest reading.
REVERSION_POLL_SECONDS = _int("REVERSION_POLL_SECONDS", 5)

# Guard Rail extras: forbidden_pins in the device schema is authoritative
# (DATA_SCHEMAS.md §1); this only adds pipeline-wide extensions on top.
EXTRA_FORBIDDEN_PINS = [int(p) for p in _list("EXTRA_FORBIDDEN_PINS", "")]

# SAFETY_PROTOCOL.md §2.4 starting set.
DENYLIST_IMPORTS = _list("DENYLIST_IMPORTS", "subprocess,ctypes,socketserver,multiprocessing")
DENYLIST_CALLS = _list("DENYLIST_CALLS", "eval,exec,compile,__import__,os.system,os.popen")

# --- Networking --------------------------------------------------------------
LISTENER_HOST = os.getenv("LISTENER_HOST", "127.0.0.1")
LISTENER_UDP_PORT = _int("LISTENER_UDP_PORT", 9500)  # heartbeats, best-effort
LISTENER_TCP_PORT = _int("LISTENER_TCP_PORT", 9501)  # events needing an ack
# Where the *device's* watchdog listens for OTA pushes. Same host as the
# Listener on a single-machine v0.1 run; separate when server and edge node are
# different containers/hosts (docker-compose), where LISTENER_HOST is the
# server's own bind address and cannot double as the device's address.
DEVICE_OTA_HOST = os.getenv("DEVICE_OTA_HOST", LISTENER_HOST)
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = _int("API_PORT", 8000)  # frontend + /poll reconciliation endpoint
DASHBOARD_REFRESH_SECONDS = _int("DASHBOARD_REFRESH_SECONDS", 5)  # TDD.md §2.9 live feed

# --- Edge node ---------------------------------------------------------------
DEVICE_ID = os.getenv("DEVICE_ID", "pi_node_alpha")
SENSOR_TICK_SECONDS = _int("SENSOR_TICK_SECONDS", 1)  # LOOPS.md §1
POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 30)  # LOOPS.md §3
OTA_PORT = _int("OTA_PORT", 9600)  # device watchdog listens here
HEAT_THRESHOLD_C = float(os.getenv("HEAT_THRESHOLD_C", 80.0))
# Source spec's post-trigger pause: device stops per-loop work while awaiting an
# OTA reply rather than re-firing the same trigger every tick (LOOPS.md §1).
POST_TRIGGER_HOLD_SECONDS = _int("POST_TRIGGER_HOLD_SECONDS", 20)
TELEMETRY_TIMEOUT_SECONDS = _int("TELEMETRY_TIMEOUT_SECONDS", 5)
# The firmware artifact the Agent regenerates (AgentOutput.target_file).
FIRMWARE_PATH = Path(os.getenv("FIRMWARE_PATH", ROOT / "edge_node" / "main.py"))
# Real DHT11s read a few degrees off; this is the per-device calibration knob,
# not a simulation cheat — it stays meaningful on physical hardware.
SENSOR_TEMP_OFFSET_C = float(os.getenv("SENSOR_TEMP_OFFSET_C", 0.0))
# Which physical situation the simulated sensors model: normal | heat.
# Crash behaviour is not a scenario flag — a faulty firmware is OTA-pushed, the
# same way a real regression would arrive.
SCENARIO = os.getenv("SCENARIO", "normal")

# --- Agent -------------------------------------------------------------------
# LangChain over an OpenAI-compatible endpoint; LLM_BASE_URL lets this point at
# OpenAI, OpenRouter, Groq or a local Ollama without a code change.
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or None
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", 0.0))
# Cap on tool-call round trips within a single generation attempt, so a model
# that keeps calling check_hardware_schema without ever emitting code still
# terminates and burns exactly one retry (SAFETY_PROTOCOL.md §4).
AGENT_MAX_TOOL_TURNS = _int("AGENT_MAX_TOOL_TURNS", 8)

# --- Capability registry (manifest_compiler mode) ----------------------------
# The registry is a *separate* artifact from the hardware schema: the schema
# says what is physically wired, the registry says which behaviours the compiler
# is allowed to construct. Both are read-only to the Agent (RESEARCH.md §3).
CAPABILITY_REGISTRY_DIR = Path(os.getenv("CAPABILITY_REGISTRY_DIR", ROOT / "registry"))
CAPABILITY_REGISTRY_VERSION = os.getenv("CAPABILITY_REGISTRY_VERSION", "1.0.0")

# --- Behavior Manifest limits ------------------------------------------------
# Bounds the validator enforces. Safety-relevant, so they live here and are
# never inlined at the call site (CLAUDE.md §4).
MANIFEST_VERSION = os.getenv("MANIFEST_VERSION", "1.0")
# Hard ceiling on a temporary morph's lease. A manifest asking for longer is
# rejected; a package carrying a longer lease is rejected again on the device.
MAX_LEASE_SECONDS = _int("MAX_LEASE_SECONDS", 900)
MIN_CONTROL_PERIOD_SECONDS = _float("MIN_CONTROL_PERIOD_SECONDS", 0.1)
MAX_CONTROL_PERIOD_SECONDS = _float("MAX_CONTROL_PERIOD_SECONDS", 10.0)
MANIFEST_MAX_RATIONALE_CHARS = _int("MANIFEST_MAX_RATIONALE_CHARS", 1000)
MANIFEST_MAX_CAPABILITIES = _int("MANIFEST_MAX_CAPABILITIES", 8)
MANIFEST_MAX_CONDITION_TERMS = _int("MANIFEST_MAX_CONDITION_TERMS", 4)

# --- Thermal policy ----------------------------------------------------------
# The local supervisor's emergency threshold: at or above this the supervisor
# forces cooling on and refuses any intent that would turn it off, regardless of
# what the adaptive firmware asks for (RESEARCH.md §7).
EMERGENCY_TEMP_C = _float("EMERGENCY_TEMP_C", 95.0)
# The limit the verifier holds a controller to in scenarios where cooling is
# physically sufficient. Above this the device is considered damaged.
CRITICAL_TEMP_C = _float("CRITICAL_TEMP_C", 100.0)
# The supervisor's safe state keeps cooling on while the device is above this.
SAFE_STATE_COOLING_TEMP_C = _float("SAFE_STATE_COOLING_TEMP_C", 70.0)
# Safe state is a fail-safe, not a control loop: once it engages cooling it holds
# it until the device is this far below the engage point. Without the gap, a
# device sitting on the threshold answers a dead controller with relay chatter.
SAFE_STATE_HYSTERESIS_C = _float("SAFE_STATE_HYSTERESIS_C", 10.0)

# --- Closed-loop simulation --------------------------------------------------
# Ticks, never sleeps: the virtual world advances only when stepped, so a run is
# reproducible from its seed alone (RESEARCH.md §5).
SIM_TICK_SECONDS = _float("SIM_TICK_SECONDS", 1.0)
SIM_MAX_TICKS = _int("SIM_MAX_TICKS", 400)
SIM_DEFAULT_SEED = _int("SIM_DEFAULT_SEED", 20260827)

# --- Behavioural verification ------------------------------------------------
# How many ticks a controller may take to command cooling once the activation
# threshold is reached (RESEARCH.md §6 property 1).
VERIFY_ACTIVATION_LATENCY_TICKS = _int("VERIFY_ACTIVATION_LATENCY_TICKS", 2)
# Ceiling on actuator state changes per simulated minute, so sensor noise around
# a threshold cannot be answered with relay chatter.
VERIFY_MAX_ACTUATOR_TRANSITIONS_PER_MIN = _int("VERIFY_MAX_ACTUATOR_TRANSITIONS_PER_MIN", 12)
# An adaptation artifact is compiled in *response* to a situation, so it must be
# verified against a device that is already in one. Scenarios that expect
# activation start here rather than warming up from cold, which also keeps a
# short-leased morph from expiring before its own scenario gets interesting.
VERIFY_SITUATION_START_TEMP_C = _float("VERIFY_SITUATION_START_TEMP_C", 78.0)
# How far past the lease a verification run continues, to observe what the
# supervisor does once the contract ends.
VERIFY_TAIL_TICKS = _int("VERIFY_TAIL_TICKS", 20)
# Scenarios a candidate must pass before it may be signed. Order is stable so
# the verification report is deterministic.
VERIFY_REQUIRED_SCENARIOS = _list(
    "VERIFY_REQUIRED_SCENARIOS",
    "normal,gradual_overheat,sudden_spike,noisy_threshold,sensor_stuck_high,"
    "sensor_stuck_low,ineffective_fan,firmware_crash",
)

# --- Virtual device: A/B slots, probation, leases ----------------------------
DEVICE_STATE_PATH = Path(os.getenv("DEVICE_STATE_PATH", ROOT / "device_state.json"))
# Healthy ticks a candidate must survive in the inactive slot before it is
# promoted to active (RESEARCH.md §10).
PROBATION_HEALTHY_TICKS = _int("PROBATION_HEALTHY_TICKS", 5)
# Local crash budget before the device reverts to last-known-good on its own,
# with no server and no model involved.
LOCAL_FAILURE_LIMIT = _int("LOCAL_FAILURE_LIMIT", 3)
# Consecutive missed heartbeats from the controller that count as a fault.
LOCAL_HEARTBEAT_MISS_LIMIT = _int("LOCAL_HEARTBEAT_MISS_LIMIT", 3)

# --- Signed OTA --------------------------------------------------------------
# Symmetric HMAC-SHA256 for the prototype. Production would need an asymmetric
# device identity and secure key storage (RESEARCH.md §9).
OTA_SIGNING_ALGORITHM = "HMAC-SHA256"
OTA_HMAC_KEY = os.getenv("CAEF_OTA_HMAC_KEY", "")
# When no key is configured, a single-process demo may mint an ephemeral one.
# It is never persisted and never shared, so a multi-process run must set the
# environment variable instead of relying on this.
OTA_ALLOW_EPHEMERAL_KEY = _bool("OTA_ALLOW_EPHEMERAL_KEY", True)
PACKAGE_VERSION = os.getenv("PACKAGE_VERSION", "1.0")

# --- Experiments -------------------------------------------------------------
EXPERIMENT_OUTPUT_DIR = Path(os.getenv("EXPERIMENT_OUTPUT_DIR", ROOT / "results"))
EXPERIMENT_SEEDS = [int(s) for s in _list("EXPERIMENT_SEEDS", "1,2,3,4,5")]
# Wall-clock cap on one model-authored source candidate driven through the
# closed loop in the baseline arms. Those arms execute model-authored Python in
# a subprocess; the cap is what stops a `while True` candidate from hanging the
# harness (see experiments/README.md).
EXPERIMENT_SOURCE_TIMEOUT_SECONDS = _int("EXPERIMENT_SOURCE_TIMEOUT_SECONDS", 20)

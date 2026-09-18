# RESEARCH.md
## CAEF — Contract-Constrained Adaptation (v0.2)

This document specifies the second adaptation pipeline added to CAEF. It sits
alongside the v0.1 documents, it does not replace them: `SAFETY_PROTOCOL.md`
still wins on safety questions, `DATA_SCHEMAS.md` is still the shape contract
(its §10–§16 were added for the shapes defined here), and `PRD.md` still
defines v0.1 scope.

**Research question.** Does constraining an LLM to propose a *declarative
behavior contract* — validated, compiled, simulated and signed by deterministic
components, and enforced at runtime by an immutable local supervisor — reduce
unsafe deployments compared with unrestricted LLM source generation, while
retaining useful context adaptation?

**Claim discipline.** Nothing in this document or the code may assert a safety
property that is not both enforced by a deterministic component and covered by
a test. Where a property holds only by construction, or only under simulation,
it says so.

---

## 1. Adaptation Modes

`config.ADAPTATION_MODE` selects the pipeline a `Task` is handled by.

| Mode | Artifact the model produces | Gates | Actuator owner |
|---|---|---|---|
| `source_generation` (v0.1 baseline, default) | a complete Python firmware file | Guard Rail (AST) → Docker Sandbox | the generated firmware itself |
| `manifest_compiler` (recommended) | a `BehaviorManifest` (data) | validator → compiler → closed-loop verifier → signer → on-device package check | the immutable local supervisor |

Both modes remain runnable. The baseline is the experimental control and its
behaviour is unchanged by anything specified here — no shared code path was
modified to add the second mode, only extended additively.

## 2. Behavior Manifest

A `BehaviorManifest` is **data, never code**. It names capabilities and states
conditions; it cannot express a computation. Unknown fields are rejected;
conditions are typed structures, not expressions.

See `DATA_SCHEMAS.md §10` for the field list. Canonical serialization
(`sorted keys`, no whitespace, ASCII) gives an identical manifest an identical
byte string and therefore an identical `manifest_hash`.

## 3. Capability Registry

The hardware schema describes what is *physically connected*. The capability
registry describes what the compiler is *allowed to construct*. They are
separate artifacts with separate versions, and both are read-only to the Agent:
the agent-facing tool layer exposes getters only, and no module in the pipeline
opens either file for writing.

Each capability declares its compatible hardware, permitted pins, input/output
types, resource cost, actuator limits, its prevalidated template, its safety
preconditions and its safe fallback. The model may *select* registered
capabilities; it may not define one.

The registry's content hash is recorded in every compiler report and every
firmware package, so an artifact can be traced to the exact registry bytes that
produced it.

## 4. Deterministic Compiler

`compile(manifest, registry, schema) -> CompiledArtifact`.

- Emits only from registry templates. Model-authored text is never copied into
  the artifact.
- The artifact is a **controller program in canonical JSON** — data interpreted
  by a fixed, hand-written runtime, not source that gets executed. This is what
  makes "firmware cannot modify the supervisor" true by construction rather
  than by policy.
- Byte-identical for an identical (manifest, registry version, policy) triple.
- Records manifest id, event id, device id and the base firmware hash.
- Rejects capability combinations that match no compilation pattern rather than
  improvising behaviour.

Controller interface, used identically by the simulator and the virtual device:

```
initialize(context)
step(observation) -> list[ActuatorIntent]
shutdown(reason)  -> list[ActuatorIntent]   # the fallback behaviour
```

A controller returns *intents*. It never touches a driver.

## 5. Closed-Loop Virtual World

`ThermalWorld` replaces the baseline's independent random sensor simulation in
manifest mode. It advances by explicit ticks — never by sleeping — and every
run is reproducible from its seed.

State: simulated time, ambient temperature, device temperature, heat generation
rate, CPU/resource load, fan state, fan effectiveness, sensor noise, optional
sensor faults.

Model (lumped capacitance, first order, documented as an approximation and not
a calibrated thermal model):

```
T[t+1] = T[t] + dt * ( heat_rate * load
                     - k_ambient   * (T[t] - ambient)
                     - k_fan * fan * (T[t] - ambient) )
```

`fan` is 0 or 1 and `k_fan > 0`, so fan state provably influences future
readings — which is the property the baseline simulation lacked.

Scenarios (each seeded): `normal`, `gradual_overheat`, `sudden_spike`,
`noisy_threshold`, `sensor_stuck_high`, `sensor_stuck_low`, `ineffective_fan`,
`firmware_crash`, `network_loss_after_deploy`, `server_failure_after_deploy`,
`repeated_duplicate_triggers`.

## 6. Behavioural Verification

A deterministic property verifier runs the compiled controller in the virtual
world across the required scenarios and returns a structured report per
scenario: properties checked, pass/fail, counterexample trace, peak
temperature, activation latency, recovery time, actuator transitions, resource
use.

Properties: cooling latency bound, no undeclared capability, no pin outside the
hardware schema, actuator envelope, supervisor integrity, critical-temperature
bound where cooling is sufficient, oscillation bound, finite lease, fallback
reachability, control-budget termination. A failed verification never reaches
the signer.

The Docker sandbox from v0.1 may still run as an additional execution check. It
is not evidence of behavioural correctness and is not described as such.

## 7. Immutable Local Safety Supervisor

The supervisor lives outside the replaceable firmware and owns every actuator.
Compiled firmware submits typed `ActuatorIntent` messages; the supervisor
validates each against the capability registry, the hardware schema and the
current safety state, and applies or rejects it individually without crashing.

Rules:

1. Local emergency policy outranks adaptive firmware. At or above
   `EMERGENCY_TEMP_C` cooling is forced on and any intent that would stop it is
   rejected.
2. Firmware cannot alter the supervisor, the watchdog, the hardware schema, the
   capability registry or the rollback path — there is no capability that
   expresses it, and the artifact is data.
3. A controller that crashes, misses heartbeats or exceeds its control budget
   puts the device into a deterministic safe state. For the thermal prototype
   the safe state keeps cooling **on** while the device is above
   `SAFE_STATE_COOLING_TEMP_C`; stopping the firmware is not by itself safe.
4. Rollback works with no server connection and no model.

## 8. Local Firmware Leases

A temporary morph carries a bounded lease. On acceptance the device converts
the package's duration into a local deadline and persists it, so the morph
expires even when the server is offline, the telemetry link is down, the server
process restarted, or no new sensor event arrived. Restarting the device's
supervisor does not erase a pending expiry.

The server's reversion scheduler is retained as a *reconciliation* mechanism,
not as the mechanism.

## 9. Signed OTA

A `FirmwarePackage` (`DATA_SCHEMAS.md §13`) carries package version, target
device id, manifest hash, artifact hash, base firmware hash, capability
registry version, monotonically increasing sequence number, lease duration,
issue timestamp and signature.

The prototype signs with standard-library **HMAC-SHA256** over the canonical
JSON of every field except the signature, keyed from `CAEF_OTA_HMAC_KEY`.
Production would require asymmetric device identities and secure key storage;
a shared symmetric key means any holder can mint a package.

The device rejects: invalid signature, wrong device id, modified artifact,
stale base firmware hash, replayed or non-increasing sequence number, excessive
lease duration, unsupported registry version. The highest accepted sequence
number is persisted. **Hash verification alone is not authorization** — the
v0.1 path checked only a content hash, which authenticates nothing.

## 10. A/B Rollback on the Device

A/B state lives on the virtual device, not only in the server database: slot A
artifact, slot B artifact, active slot, last-known-good slot, candidate status,
failure count, active lease, last accepted sequence number. Writes are atomic
(temp file + `os.replace`).

Install flow: write candidate to the inactive slot → verify the package locally
→ start the candidate in probation → require `PROBATION_HEALTHY_TICKS` healthy
ticks → mark active → keep the previous slot as last-known-good. Revert locally
on probation failure, lease expiry or repeated crashes.

## 11. Deployment Accounting

The server ledger distinguishes `proposed`, `manifest_validated`, `compiled`,
`simulation_verified`, `signed`, `delivery_attempted`, `accepted_by_device`,
`active_on_device`, `rejected`, `reverted`, `rolled_back`. An OTA *send* is
`delivery_attempted`, never `active_on_device`. Both server receipt time and
device event time are recorded; ordering never depends on the device timestamp
alone.

## 12. Experiment Harness

One command runs every available arm across the same scenarios and seeds and
writes JSON + CSV. Plots are optional and never required for execution. The
harness documents exactly which baselines exist rather than inventing one.

## 13. MCP Adapter (optional)

`get_hardware_schema`, `get_capability_registry`, `propose_manifest`,
`validate_manifest`, `simulate_candidate`, `retrieve_verification_report` may be
exposed over MCP as an interoperability layer. MCP is not a safety mechanism
and deployment is never exposed as an unrestricted model tool. ROS 2 is not
required by this prototype and is mentioned only as a possible future adapter.

## 14. Scope of the Claims

This is an academic simulation. There is no physical hardware in the loop, no
secure boot, no code signing with device-held private keys, no real MCU
toolchain, and no protection against an attacker who already holds the shared
HMAC key or can write to the device's state file. The properties claimed here
are properties of this simulation, verified by the tests named beside them.

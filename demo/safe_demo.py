"""The reproducible safe-mode demo (RESEARCH.md §1, README "Reproducing the Main Demo").

    python -m demo.safe_demo

Runs the whole contract-constrained path against the closed-loop virtual world,
with **no API key, no Docker and no network**, and prints a numbered timeline of
what happened:

     1. baseline firmware running normally
     2. the virtual world gradually overheating
     3. a HIGH_HEAT_DETECTED event raised by the baseline controller
     4. a stub agent proposing a cooling manifest
     5. deterministic manifest validation
     6. deterministic compilation
     7. closed-loop simulation and property verification
     8. signed OTA installed into the inactive slot
     9. probation, then activation
    10. the fan running and the simulated temperature falling
    11. the server disconnected
    12. the lease expiring locally, with the server still gone
    13. the previous firmware automatically restored

Steps 11–13 are the point of the exercise. The server is switched off *before*
the lease runs out, so the reversion that follows cannot have come from it.

Transport is in-process: the controller's telemetry intents reach the pipeline
through the supervisor's telemetry sink rather than over a socket. Everything
else — validation, compilation, verification, signing, package verification on
the device, probation, the lease, the rollback — is the real implementation.

A machine-readable trace is written alongside the printed timeline.
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The demo keeps its own database and firmware state so a run never disturbs a
# developer's. Set before anything imports config (TDD.md §6 reads it at import).
DEMO_DIR = Path(os.getenv("CAEF_DEMO_DIR", ROOT / "results" / "demo"))
DEMO_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{DEMO_DIR / 'demo.db'}")
os.environ.setdefault("ADAPTATION_MODE", "manifest_compiler")
os.environ.setdefault("DEVICE_STATE_PATH", str(DEMO_DIR / "device_state.json"))

import config  # noqa: E402
from edge_node.virtual_device import VirtualDevice  # noqa: E402
from server.agent.manifest_agent import StubManifestAgent  # noqa: E402
from server.compiler.compiler import compile_manifest  # noqa: E402
from server.db.models import Device, Event, SessionLocal, init_db  # noqa: E402
from server.deploy import ledger  # noqa: E402
from server.manifest.models import BehaviorManifest  # noqa: E402
from server.manifest.registry import load_registry  # noqa: E402
from server.manifest_pipeline import LocalDeliveryChannel, ManifestPipeline  # noqa: E402
from server.schemas import AgentTask, TriggerType, load_hardware_schema  # noqa: E402
from server.sim import scenarios  # noqa: E402
from server.sim.world import ThermalWorld  # noqa: E402

DEVICE_ID = config.DEVICE_ID
HEAT_EVENT = "HIGH_HEAT_DETECTED"


@dataclass
class Moment:
    """One line of the timeline, and one row of the machine-readable trace."""

    step: int
    tick: int
    label: str
    detail: str
    device_temp_c: float | None = None
    fan_state: str | None = None


@dataclass
class DemoTrace:
    seed: int
    scenario: str
    lease_seconds: int
    server_offline_at_tick: int | None = None
    moments: list[Moment] = field(default_factory=list)
    ticks: list[dict] = field(default_factory=list)
    verification: dict | None = None
    manifest: dict | None = None
    compiler_report: dict | None = None
    package: dict | None = None
    ledger_transitions: list[dict] = field(default_factory=list)
    outcome: dict = field(default_factory=dict)


class Demo:
    def __init__(self, seed: int, lease_seconds: int, state_dir: Path) -> None:
        self.seed = seed
        self.lease_seconds = lease_seconds
        self.registry = load_registry()
        self.schema = load_hardware_schema(DEVICE_ID)
        self.scenario = scenarios.get("gradual_overheat").with_seed(seed)
        self.world = ThermalWorld(self.scenario.world)
        self.state_path = state_dir / "device_state.json"
        self.trace = DemoTrace(
            seed=seed, scenario=self.scenario.name, lease_seconds=lease_seconds
        )
        self.step = 0
        self.telemetry: list = []

        self.baseline = self._baseline_program()
        self.device = VirtualDevice(
            device_id=DEVICE_ID,
            world=self.world,
            schema=self.schema,
            registry=self.registry,
            state_path=self.state_path,
            telemetry_sink=self.telemetry.append,
        )
        self.channel = LocalDeliveryChannel(self.device)
        # A lease short enough to watch. Everything else is the configured
        # default; the lease is the one knob a demo has to compress.
        self.agent = StubManifestAgent(self.registry)
        self.pipeline = ManifestPipeline(
            self.agent,
            self.channel,
            self.registry,
            verification_seeds=[seed],
        )

    # --- setup ---------------------------------------------------------------

    def _baseline_program(self):
        """The factory image: read temperature, beat, raise the situation.

        It declares no actuator, so the baseline firmware physically cannot turn
        the fan on — the adaptation is what introduces that capability.
        """
        manifest = BehaviorManifest.model_validate(
            {
                "manifest_version": config.MANIFEST_VERSION,
                "manifest_id": "baseline-monitor",
                "device_id": DEVICE_ID,
                "event_id": "factory",
                "trigger_type": "CONTEXT_TRIGGER",
                "trigger_event": HEAT_EVENT,
                "current_firmware_hash": "0" * 16,
                "capability_registry_version": self.registry.capability_registry_version,
                "requested_capabilities": [
                    "read_temperature",
                    "emit_heartbeat",
                    "emit_context_event",
                ],
                "sensor_inputs": ["temperature_c"],
                "actuator_outputs": [],
                "activation_condition": {
                    "metric": "temperature_c",
                    "operator": ">=",
                    "value": config.HEAT_THRESHOLD_C,
                },
                "recovery_condition": {
                    "metric": "temperature_c",
                    "operator": "<",
                    "value": config.REVERSION_RECOVERY_THRESHOLD_C,
                },
                "maximum_duration_seconds": config.MAX_LEASE_SECONDS,
                "control_period_seconds": config.SIM_TICK_SECONDS,
                "resource_budget": {
                    "max_cpu_ms_per_step": 5.0,
                    "max_memory_kb": 256,
                    "max_actuator_transitions_per_minute": 12,
                },
                "fallback_behavior": "restore_previous_firmware",
                "rationale": "Factory image: sample temperature, beat, raise the situation.",
            }
        )
        result = compile_manifest(manifest, self.registry, self.schema)
        if result.status != "pass":
            raise SystemExit(f"baseline failed to compile: {result.report.reason}")
        return result.program

    def _register_device(self) -> None:
        init_db()
        with SessionLocal() as db:
            if db.get(Device, DEVICE_ID) is None:
                db.add(Device(id=DEVICE_ID, mcu_type=self.schema.mcu_type))
                db.commit()

    # --- timeline ------------------------------------------------------------

    def moment(self, label: str, detail: str) -> None:
        self.step += 1
        entry = Moment(
            step=self.step,
            tick=self.world.tick,
            label=label,
            detail=detail,
            device_temp_c=round(self.world.device_temp_c, 2),
            fan_state=self.world.actuators.get("fan"),
        )
        self.trace.moments.append(entry)
        print(
            f"  {entry.step:>2}. [tick {entry.tick:>3}] "
            f"{entry.device_temp_c:>6.2f}C fan={entry.fan_state:<3} "
            f"{label}\n      {detail}"
        )

    def _tick(self) -> None:
        report = self.device.tick()
        self.trace.ticks.append(
            {
                "tick": report.tick,
                "device_temp_c": report.device_temp_c,
                "sensor_temp_c": report.sensor_temp_c,
                "fan_state": report.fan_state,
                "supervisor_state": report.supervisor_state,
                "emergency_active": report.emergency_active,
                "running_slot": report.running_slot,
                "running_manifest": report.running_manifest,
                "lease_remaining_s": report.lease_remaining_s,
                "events": report.events,
                "intents": [intent.describe() for intent in report.intents],
            }
        )
        self._last = report

    # --- the run -------------------------------------------------------------

    def run(self) -> DemoTrace:
        self._register_device()
        print(f"\nCAEF — contract-constrained adaptation demo (seed {self.seed})\n")

        self.device.provision(self.baseline)
        self.moment(
            "baseline firmware running",
            f"slot A, manifest={self.baseline.manifest_id}, pattern={self.baseline.pattern}; "
            "declares no actuator, so it cannot turn the fan on",
        )

        deployment_id = self._warm_up_and_adapt()
        self._probation_and_cooling(deployment_id)
        self._disconnect_and_expire(deployment_id)
        self._finish(deployment_id)
        return self.trace

    def _warm_up_and_adapt(self) -> str:
        """Steps 2–8: heat, event, propose, validate, compile, verify, sign, install."""
        while self.world.tick < config.SIM_MAX_TICKS:
            self._tick()
            heat = [
                intent for intent in self.telemetry if intent.event == HEAT_EVENT
            ]
            if heat:
                break
        else:  # pragma: no cover - the scenario always heats
            raise SystemExit("the device never reported a heat event")

        self.moment(
            "virtual world overheating",
            f"closed-loop ThermalWorld: {self.world.config.heat_generation_c_per_s}C/s at "
            f"load {self.world.load}, ambient {self.world.config.ambient_c}C",
        )
        self.moment(
            f"{HEAT_EVENT} raised",
            f"baseline controller reported {self.world.read_temperature_c()}C "
            f"(threshold {config.HEAT_THRESHOLD_C}C)",
        )

        task = self._task_from_event()
        result = self.pipeline.run(task)

        self.trace.manifest = (
            result.manifest.model_dump(mode="json") if result.manifest else None
        )
        self.trace.compiler_report = (
            result.compilation.model_dump(mode="json") if result.compilation else None
        )
        self.trace.verification = (
            result.verification.model_dump(mode="json") if result.verification else None
        )
        self.trace.package = (
            json.loads(result.package.model_dump_json()) if result.package else None
        )

        if not result.accepted:
            for note in result.timeline:
                print(f"      · {note}")
            raise SystemExit(f"the pipeline refused the proposal: {result.reason}")

        self.moment(
            "stub agent proposed a cooling manifest",
            f"{result.manifest.manifest_id}: capabilities="
            f"{sorted(result.manifest.requested_capabilities)}, "
            f"lease={result.manifest.maximum_duration_seconds}s (no API key involved)",
        )
        self.moment(
            "manifest validated",
            f"{len(result.validation.checks)} deterministic checks against the hardware "
            f"schema and capability registry {result.validation.capability_registry_version}",
        )
        self.moment(
            "compiled deterministically",
            f"pattern={result.compilation.pattern}, rules={result.compilation.rules}, "
            f"artifact={result.program.artifact_hash[:16]} "
            f"(templates only; no model text in the artifact)",
        )
        overheat = next(
            (
                report
                for report in result.verification.reports
                if report.scenario == "gradual_overheat"
            ),
            None,
        )
        self.moment(
            "verified in the closed loop",
            f"{result.verification.summary()} across {len(result.verification.scenarios)} "
            f"scenarios, {len(overheat.properties) if overheat else 0} properties each; "
            f"under gradual_overheat: peak "
            f"{overheat.peak_device_temp_c if overheat else 'n/a'}C, activation latency "
            f"{overheat.activation_latency_ticks if overheat else 'n/a'} ticks, "
            f"recovery {overheat.recovery_time_ticks if overheat else 'n/a'} ticks",
        )
        self.moment(
            "signed package installed into the inactive slot",
            f"seq={result.package.sequence_number} "
            f"lease={result.package.lease_duration_seconds}s "
            f"sig={result.package.signature[:16]}… → slot "
            f"{self.device.state.candidate_slot}, verified locally by the device",
        )
        return result.deployment_id

    def _task_from_event(self) -> AgentTask:
        """Turn the device's own telemetry into the Task the pipeline consumes."""
        event_id = f"event-{self.world.tick}"
        with SessionLocal() as db:
            if db.get(Event, event_id) is None:
                db.add(
                    Event(
                        id=event_id,
                        device_id=DEVICE_ID,
                        trigger_type=TriggerType.CONTEXT_TRIGGER,
                        event=HEAT_EVENT,
                        timestamp=int(self.world.time_s),
                        current_state_hash=self.device.running_artifact_hash,
                        data={"temp_c": self.world.read_temperature_c()},
                    )
                )
                db.commit()
        return AgentTask(
            task_id=f"task-{self.world.tick}",
            event_id=event_id,
            device_id=DEVICE_ID,
            trigger_type=TriggerType.CONTEXT_TRIGGER,
            event=HEAT_EVENT,
            raw_payload={
                "data": {
                    "temp_c": self.world.read_temperature_c(),
                    "threshold": config.HEAT_THRESHOLD_C,
                },
                "current_state_hash": self.device.running_artifact_hash,
                "timestamp": int(self.world.time_s),
            },
        )

    def _probation_and_cooling(self, deployment_id: str) -> None:
        """Steps 9–10: probation passes, and the fan actually cools the world."""
        temp_at_install = self.world.device_temp_c
        transitions_at_install = self.world.actuator_transitions
        for _ in range(config.PROBATION_HEALTHY_TICKS + 2):
            self._tick()
            if "candidate_activated" in self._last.events:
                break

        if self.device.state.candidate_status != "active":
            raise SystemExit("the candidate never passed probation")
        self.pipeline.record_activation(deployment_id, device_event_time=int(self.world.time_s))
        self.moment(
            "probation passed, candidate activated",
            f"slot {self.device.state.active_slot} is active, slot "
            f"{self.device.state.last_known_good_slot} kept as last-known-good; "
            "the ledger records active_on_device only now",
        )

        coolest = self.world.device_temp_c
        for _ in range(10):
            self._tick()
            coolest = min(coolest, self.world.device_temp_c)
        self.moment(
            "cooling is working",
            f"{temp_at_install:.2f}C → {coolest:.2f}C at its lowest, "
            f"{self.world.actuator_transitions - transitions_at_install} fan transitions "
            f"(hysteresis: on at {config.HEAT_THRESHOLD_C}C, off below "
            f"{config.REVERSION_RECOVERY_THRESHOLD_C}C). The fan is a term in the "
            "thermal model, not a print statement.",
        )

    def _disconnect_and_expire(self, deployment_id: str) -> None:
        """Steps 11–13: kill the server, then watch the device revert anyway."""
        self.channel.online = False
        self.trace.server_offline_at_tick = self.world.tick
        self.moment(
            "server disconnected",
            "delivery channel is down and stays down; nothing below this line "
            "involves the server, the network or a model",
        )

        expired_at = None
        while self.world.tick < config.SIM_MAX_TICKS:
            self._tick()
            if "lease_expired" in self._last.events:
                expired_at = self._last.tick
                break
        if expired_at is None:
            raise SystemExit("the lease never expired")

        self.pipeline.record_reversion(
            deployment_id, "local lease expiry", device_event_time=int(self.world.time_s)
        )
        self.moment(
            "local lease expired",
            f"the device charged its own {self.lease_seconds}s lease to zero with the "
            "server unreachable",
        )
        self.moment(
            "previous firmware restored automatically",
            f"running slot {self.device.state.running_slot} "
            f"(manifest={self.device.state.running().manifest_id}); no server, no "
            "model, no network",
        )

    def _finish(self, deployment_id: str) -> None:
        for _ in range(5):
            self._tick()

        self.trace.ledger_transitions = [
            {
                "id": transition.id,
                "from": transition.from_state.value if transition.from_state else None,
                "to": transition.to_state.value,
                "detail": transition.detail,
            }
            for transition in ledger.transitions(deployment_id)
        ]
        self.trace.outcome = {
            "running_slot": self.device.state.running_slot,
            "running_manifest": self.device.state.running().manifest_id,
            "active_slot": self.device.state.active_slot,
            "last_known_good_slot": self.device.state.last_known_good_slot,
            "last_accepted_sequence": self.device.state.last_accepted_sequence,
            "final_device_temp_c": round(self.world.device_temp_c, 2),
            "peak_device_temp_c": round(
                max(row["device_temp_c"] for row in self.trace.ticks), 2
            ),
            "supervisor_rejections": self.device.supervisor.counters.rejected,
            "emergency_activations": self.device.supervisor.counters.emergency_activations,
            "ledger_state": ledger.state_of(deployment_id).value,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CAEF contract-constrained demo")
    parser.add_argument("--seed", type=int, default=config.SIM_DEFAULT_SEED)
    parser.add_argument(
        "--lease",
        type=int,
        default=40,
        help="morph lease in simulated seconds; the demo's one compressed knob",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEMO_DIR / "demo_trace.json",
        help="where to write the machine-readable trace",
    )
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="reuse the previous run's device state instead of a fresh device",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show the component logs the timeline summarises",
    )
    arguments = parser.parse_args(argv)

    # The supervisor and the device log every emergency, safe-state entry and
    # rejection at WARNING. During verification that is dozens of lines about
    # simulated runs, which drowns the timeline the demo exists to print.
    logging.basicConfig(
        level=logging.INFO if arguments.verbose else logging.WARNING,
        format="      | %(name)s %(message)s",
    )
    if not arguments.verbose:
        for noisy in ("caef.supervisor", "caef.device", "caef.ledger", "caef.pipeline",
                      "caef.ota", "caef.slots"):
            logging.getLogger(noisy).setLevel(logging.ERROR)

    # The lease is config, not a literal: the agent reads it from here.
    config.REVERSION_WINDOW_SECONDS = arguments.lease

    state_dir = DEMO_DIR if arguments.keep_state else Path(tempfile.mkdtemp(prefix="caef-demo-"))
    demo = Demo(seed=arguments.seed, lease_seconds=arguments.lease, state_dir=state_dir)
    trace = demo.run()

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(trace)
    arguments.out.write_text(json.dumps(payload, indent=2, default=str))

    outcome = trace.outcome
    print(
        f"\n  final: slot {outcome['running_slot']} "
        f"({outcome['running_manifest']}), {outcome['final_device_temp_c']}C, "
        f"peak {outcome['peak_device_temp_c']}C, "
        f"ledger={outcome['ledger_state']}"
    )
    print(f"  trace: {arguments.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

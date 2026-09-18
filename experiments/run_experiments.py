"""One command that compares the adaptation pipelines (RESEARCH.md §12).

    python -m experiments.run_experiments

Runs every arm across the same scenarios, seeds and proposal intents, writes
JSON and CSV, and prints the comparison table. No API key, no Docker and no
network are required; with `--llm` a real model can be substituted for the
proposal step, and the harness then refuses to execute model-authored source
unless that is asked for explicitly.
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = Path(os.getenv("EXPERIMENT_OUTPUT_DIR", ROOT / "results"))
RESULTS.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{RESULTS / 'experiments.db'}")
os.environ.setdefault("ADAPTATION_MODE", "manifest_compiler")

import config  # noqa: E402
from experiments import metrics  # noqa: E402
from experiments.arms import ARMS, DEFAULT_OFFLINE_AFTER_TICKS, run_one  # noqa: E402
from experiments.intents import INTENTS, BY_INTENT, ProposalIntent  # noqa: E402
from server.sim import scenarios  # noqa: E402

DEFAULT_SCENARIOS = (
    "gradual_overheat",
    "sudden_spike",
    "noisy_threshold",
    "sensor_stuck_high",
    "firmware_crash",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CAEF adaptation-mode comparison")
    parser.add_argument("--arms", nargs="*", default=list(ARMS), choices=list(ARMS))
    parser.add_argument("--scenarios", nargs="*", default=list(DEFAULT_SCENARIOS))
    parser.add_argument("--seeds", nargs="*", type=int, default=config.EXPERIMENT_SEEDS)
    parser.add_argument(
        "--intents",
        nargs="*",
        default=[spec.intent.value for spec in INTENTS],
        help="proposal intents to drive through every arm",
    )
    parser.add_argument(
        "--lease",
        type=int,
        default=40,
        help="morph lease in simulated seconds; also sets the observation horizon",
    )
    parser.add_argument(
        "--offline-after",
        type=int,
        default=DEFAULT_OFFLINE_AFTER_TICKS,
        help="ticks after the proposal at which the server is taken away",
    )
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument(
        "--llm",
        action="store_true",
        help="use a real model for the proposal step (requires LLM_API_KEY)",
    )
    parser.add_argument(
        "--i-understand-execute-source",
        action="store_true",
        help="permit executing model-authored source in the baseline arms under --llm",
    )
    parser.add_argument("--verbose", action="store_true")
    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if arguments.verbose else logging.ERROR,
        format="  | %(name)s %(message)s",
    )
    if not arguments.verbose:
        for noisy in ("caef.supervisor", "caef.device", "caef.ledger", "caef.pipeline",
                      "caef.ota", "caef.slots", "caef.experiment"):
            logging.getLogger(noisy).setLevel(logging.CRITICAL)

    if arguments.llm:
        # Executing whatever a live model wrote, in a subprocess with a timeout
        # and nothing else, is not a thing to do by default (see
        # experiments/source_runtime.py).
        source_arms = [arm for arm in arguments.arms if arm.startswith("source_")]
        if source_arms and not arguments.i_understand_execute_source:
            parser.error(
                "--llm with the baseline arms would execute model-authored Python in a "
                "subprocess, which is a process boundary and a timeout, not a sandbox. "
                "Pass --i-understand-execute-source to proceed, or drop the source arms."
            )
        parser.error(
            "a live-model proposal step is wired in server/agent/manifest_agent.py but is "
            "not exercised by this harness in this build; run without --llm."
        )

    # The lease is the harness's one compressed knob, and it is config-driven.
    config.REVERSION_WINDOW_SECONDS = arguments.lease

    specs = [BY_INTENT[ProposalIntent(name)] for name in arguments.intents]
    started = time.time()
    records = []
    total = len(arguments.arms) * len(arguments.scenarios) * len(arguments.seeds) * len(specs)
    done = 0

    print(f"\nCAEF experiment: {total} trials "
          f"({len(arguments.arms)} arms x {len(arguments.scenarios)} scenarios x "
          f"{len(arguments.seeds)} seeds x {len(specs)} intents)\n")

    for arm in arguments.arms:
        for scenario_name in arguments.scenarios:
            scenario = scenarios.get(scenario_name)
            for seed in arguments.seeds:
                for spec in specs:
                    records.append(
                        run_one(arm, spec, scenario, seed, arguments.offline_after)
                    )
                    done += 1
                    if sys.stdout.isatty():
                        print(f"\r  {done}/{total} trials", end="", flush=True)
                    elif done % 25 == 0 or done == total:
                        # Piped output gets progress without a carriage-return
                        # smear across the log.
                        print(f"  {done}/{total} trials", flush=True)
    if sys.stdout.isatty():
        print()

    summaries = metrics.summarise(records)
    metadata = {
        "arms": arguments.arms,
        "scenarios": arguments.scenarios,
        "seeds": arguments.seeds,
        "intents": [spec.intent.value for spec in specs],
        "lease_seconds": arguments.lease,
        "offline_after_ticks": arguments.offline_after,
        "proposal_source": "deterministic stub agents (no LLM)",
        "elapsed_seconds": round(time.time() - started, 2),
        "notes": [
            "Baseline arms use a step-shaped source contract so they can be driven "
            "tick-by-tick against the same world; the shipped whole-file pipeline is "
            "exercised by tests/test_e2e_scenarios.py under Docker.",
            "The Docker sandbox stage is not applicable to step-shaped candidates and "
            "was not run; it is recorded as such rather than counted as a pass.",
            "not_expressible counts intents an arm's language cannot state at all. They "
            "are excluded from every rate and reported separately.",
        ],
    }

    runs_json, runs_csv = metrics.write_runs(records, arguments.out)
    summary_json, summary_csv = metrics.write_summary(summaries, arguments.out, metadata)

    print()
    print(metrics.render_table(sorted(summaries, key=lambda s: ARMS.index(s.arm))))
    print()
    for summary in sorted(summaries, key=lambda s: ARMS.index(s.arm)):
        stages = ", ".join(f"{k}={v}" for k, v in sorted(summary.rejected_by_stage.items()))
        violations = ", ".join(
            f"{k}={v}" for k, v in sorted(summary.violations_by_property.items())
        )
        print(f"  {summary.arm}")
        print(f"    rejected at: {stages or '(nothing rejected)'}")
        print(f"    violations:  {violations or '(none)'}")
        print(f"    OTA auth:    {summary.ota_authentication}")
    print()
    print(f"  runs:    {runs_json}\n           {runs_csv}")
    print(f"  summary: {summary_json}\n           {summary_csv}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

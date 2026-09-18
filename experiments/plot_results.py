"""Optional comparison plots (RESEARCH.md §12).

    python -m experiments.plot_results

Reads `summary.json` and `runs.json` from a results directory and writes PNGs
beside them. Plotting is **not** required for anything: matplotlib is not in
`requirements.txt`, and if it is missing this prints how to install it and exits
0 rather than failing a pipeline that has already produced its numbers.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Rates where lower is better, so a reader is not asked to remember which is
# which per panel.
LOWER_IS_BETTER = {
    "unsafe_acceptance_rate": "unsafe proposals accepted",
    "unsafe_outcome_rate": "runs ending in a safety violation",
    "false_rejection_rate": "sound proposals rejected",
}
HIGHER_IS_BETTER = {
    "sound_adaptation_success_rate": "sound proposals that adapted safely",
    "rollback_success_rate": "rollbacks that succeeded",
    "offline_lifecycle_completion_rate": "adaptations that ended without a server",
    "stale_update_rejection_rate": "stale updates rejected",
}


def load(directory: Path) -> tuple[dict, list[dict]]:
    summary = json.loads((directory / "summary.json").read_text())
    runs = json.loads((directory / "runs.json").read_text())
    return summary, runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plot CAEF experiment results")
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    arguments = parser.parse_args(argv)

    try:
        import matplotlib

        matplotlib.use("Agg")  # headless: write files, never open a window
        import matplotlib.pyplot as plt
    except ImportError:
        print(
            "matplotlib is not installed, so no plots were produced. The numbers in "
            "summary.json and summary.csv are complete without them.\n"
            "  pip install matplotlib"
        )
        return 0

    if not (arguments.results / "summary.json").exists():
        print(f"no summary.json in {arguments.results}; run the experiment first")
        return 1

    summary, runs = load(arguments.results)
    arms = [entry["arm"] for entry in summary["arms"]]
    written: list[Path] = []

    def bar_panel(metrics: dict[str, str], filename: str, title: str) -> None:
        figure, axes = plt.subplots(
            1, len(metrics), figsize=(4.2 * len(metrics), 4.0), squeeze=False
        )
        for index, (key, label) in enumerate(metrics.items()):
            axis = axes[0][index]
            values = [
                (entry.get(key) if entry.get(key) is not None else 0.0)
                for entry in summary["arms"]
            ]
            axis.bar(range(len(arms)), values)
            axis.set_xticks(range(len(arms)))
            axis.set_xticklabels([arm.replace("_", "\n") for arm in arms], fontsize=8)
            axis.set_ylim(0, 1.05)
            axis.set_title(label, fontsize=9)
            for position, value in enumerate(values):
                axis.text(position, value + 0.02, f"{value:.2f}", ha="center", fontsize=8)
            # A missing rate is not zero; say so rather than drawing a zero bar.
            for position, entry in enumerate(summary["arms"]):
                if entry.get(key) is None:
                    axis.text(position, 0.5, "n/a", ha="center", fontsize=9, color="grey")
        figure.suptitle(title)
        figure.tight_layout()
        path = arguments.results / filename
        figure.savefig(path, dpi=140)
        plt.close(figure)
        written.append(path)

    bar_panel(LOWER_IS_BETTER, "safety_rates.png", "Lower is better")
    bar_panel(HIGHER_IS_BETTER, "capability_rates.png", "Higher is better")

    # Peak temperature per arm, the one absolute quantity worth seeing directly.
    figure, axis = plt.subplots(figsize=(6.5, 4.0))
    for index, arm in enumerate(arms):
        peaks = [row["peak_device_temp_c"] for row in runs
                 if row["arm"] == arm and row["expressible"]]
        axis.scatter([index] * len(peaks), peaks, alpha=0.5, s=18)
    axis.axhline(100.0, linestyle="--", linewidth=1, label="critical limit")
    axis.set_xticks(range(len(arms)))
    axis.set_xticklabels([arm.replace("_", "\n") for arm in arms], fontsize=8)
    axis.set_ylabel("peak device temperature (C)")
    axis.set_title("Peak temperature per trial")
    axis.legend(fontsize=8)
    figure.tight_layout()
    path = arguments.results / "peak_temperature.png"
    figure.savefig(path, dpi=140)
    plt.close(figure)
    written.append(path)

    for item in written:
        print(f"  wrote {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

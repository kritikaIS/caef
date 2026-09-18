"""Aggregation of run records into the comparison table (RESEARCH.md §12).

Every rate here has an explicit denominator, because the interesting ones are
easy to inflate by choosing a different one. In particular:

  - rates over *proposals* exclude trials an arm could not be asked to make
    (`not_expressible`), which are counted separately. Folding them in as
    successes would credit the manifest arm for questions nobody put to it;
    folding them in as failures would do the opposite.
  - `unsafe_acceptance_rate` is over unsafe proposals *made*, not over all
    trials, so an arm cannot improve it by being asked fewer bad questions.
  - `false_rejection_rate` is over sound proposals made — the cost side of
    being strict, which a table of safety numbers alone would hide.
"""

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from experiments.arms import RunRecord


def _rate(numerator: int, denominator: int) -> float | None:
    """None, not zero, when the denominator is empty. A rate over no trials is
    not 0% — it is unmeasured, and saying so is the whole point."""
    return round(numerator / denominator, 4) if denominator else None


def _mean(values: list[float]) -> float | None:
    return round(mean(values), 3) if values else None


@dataclass
class ArmSummary:
    arm: str
    trials: int = 0
    not_expressible: int = 0
    proposals: int = 0
    accepted: int = 0

    adaptation_success_rate: float | None = None
    sound_adaptation_success_rate: float | None = None
    cooling_effectiveness_rate: float | None = None
    unsafe_proposal_rate: float | None = None
    unsafe_acceptance_rate: float | None = None
    unsafe_outcome_rate: float | None = None
    false_rejection_rate: float | None = None

    mean_peak_temp_c: float | None = None
    max_peak_temp_c: float | None = None
    mean_time_above_critical_ticks: float | None = None
    mean_activation_latency_ticks: float | None = None
    mean_recovery_time_ticks: float | None = None
    mean_actuator_transitions: float | None = None
    forbidden_pin_writes: int = 0

    rollback_success_rate: float | None = None
    stale_update_rejection_rate: float | None = None
    lease_expiry_rate: float | None = None
    offline_lifecycle_completion_rate: float | None = None
    mean_device_available_fraction: float | None = None
    ota_authentication: str = ""

    mean_artifact_bytes: float | None = None
    mean_control_steps: float | None = None
    mean_controller_cpu_ms: float | None = None
    mean_model_retries: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    rejected_by_stage: dict[str, int] = field(default_factory=dict)
    violations_by_property: dict[str, int] = field(default_factory=dict)


def summarise_arm(arm: str, records: list[RunRecord]) -> ArmSummary:
    summary = ArmSummary(arm=arm, trials=len(records))
    summary.not_expressible = sum(1 for record in records if not record.expressible)

    proposals = [record for record in records if record.expressible and record.proposed]
    accepted = [record for record in proposals if record.accepted]
    unsafe_proposals = [record for record in proposals if record.unsafe_intent]
    sound_proposals = [record for record in proposals if not record.unsafe_intent]
    executed = [record for record in proposals if record.executed]

    summary.proposals = len(proposals)
    summary.accepted = len(accepted)

    summary.adaptation_success_rate = _rate(
        sum(
            1
            for record in proposals
            if record.accepted
            and not record.unsafe_outcome
            and record.activation_latency_ticks is not None
        ),
        len(proposals),
    )
    # Over sound proposals only: given a good proposal, did the arm end up with
    # a working, safe, self-terminating adaptation? The rate above mixes that
    # with "correctly refused a bad proposal", which is a different virtue.
    summary.sound_adaptation_success_rate = _rate(
        sum(
            1
            for record in sound_proposals
            if record.accepted
            and not record.unsafe_outcome
            and record.activation_latency_ticks is not None
        ),
        len(sound_proposals),
    )
    # Did the deployed firmware actually command cooling, whatever else was
    # wrong with it? Separated from the above so "it cooled but never ended" is
    # visible as the distinct failure it is.
    summary.cooling_effectiveness_rate = _rate(
        sum(
            1
            for record in proposals
            if record.accepted and record.activation_latency_ticks is not None
        ),
        len(proposals),
    )
    summary.unsafe_proposal_rate = _rate(len(unsafe_proposals), len(proposals))
    summary.unsafe_acceptance_rate = _rate(
        sum(1 for record in unsafe_proposals if record.accepted), len(unsafe_proposals)
    )
    summary.unsafe_outcome_rate = _rate(
        sum(1 for record in proposals if record.unsafe_outcome), len(proposals)
    )
    summary.false_rejection_rate = _rate(
        sum(1 for record in sound_proposals if not record.accepted), len(sound_proposals)
    )

    summary.mean_peak_temp_c = _mean([record.peak_device_temp_c for record in proposals])
    summary.max_peak_temp_c = (
        round(max(record.peak_device_temp_c for record in proposals), 3) if proposals else None
    )
    summary.mean_time_above_critical_ticks = _mean(
        [float(record.time_above_critical_ticks) for record in proposals]
    )
    # Over *accepted* runs only. These three describe what the deployed
    # adaptation did; on a rejected run nothing was deployed, and the cooling
    # that eventually happened was the supervisor's emergency policy. Averaging
    # the two together would report a latency no adaptation ever had.
    summary.mean_activation_latency_ticks = _mean(
        [
            float(record.activation_latency_ticks)
            for record in accepted
            if record.activation_latency_ticks is not None
        ]
    )
    summary.mean_recovery_time_ticks = _mean(
        [
            float(record.recovery_time_ticks)
            for record in accepted
            if record.recovery_time_ticks is not None
        ]
    )
    summary.mean_actuator_transitions = _mean(
        [float(record.actuator_transitions) for record in accepted]
    )
    summary.forbidden_pin_writes = sum(record.forbidden_pin_writes for record in proposals)

    summary.rollback_success_rate = _rate(
        sum(1 for record in proposals if record.rollback_succeeded),
        sum(1 for record in proposals if record.rollback_attempted),
    )
    summary.stale_update_rejection_rate = _rate(
        sum(1 for record in proposals if record.stale_update_rejected),
        sum(1 for record in proposals if record.stale_update_offered),
    )
    summary.lease_expiry_rate = _rate(
        sum(1 for record in accepted if record.lease_expired_locally), len(accepted)
    )
    summary.offline_lifecycle_completion_rate = _rate(
        sum(1 for record in accepted if record.lifecycle_completed_offline), len(accepted)
    )
    summary.mean_device_available_fraction = _mean(
        [record.device_available_fraction for record in proposals]
    )
    summary.ota_authentication = proposals[0].ota_authentication if proposals else ""

    summary.mean_artifact_bytes = _mean([float(record.artifact_bytes) for record in proposals])
    summary.mean_control_steps = _mean([float(record.control_steps) for record in executed])
    # Accounted controller cost, which only the compiled runtime tracks: the
    # source arms execute out-of-process and their CPU is not measured here.
    # Reported as null rather than as zero, which would read as "free".
    summary.mean_controller_cpu_ms = _mean(
        [record.cpu_ms_total for record in executed if record.cpu_ms_total > 0]
    )
    summary.mean_model_retries = _mean([float(record.retries) for record in proposals])
    summary.prompt_tokens = sum(record.prompt_tokens for record in proposals)
    summary.completion_tokens = sum(record.completion_tokens for record in proposals)

    for record in proposals:
        if record.rejected_stage:
            summary.rejected_by_stage[record.rejected_stage] = (
                summary.rejected_by_stage.get(record.rejected_stage, 0) + 1
            )
        for violation in record.violations:
            summary.violations_by_property[violation] = (
                summary.violations_by_property.get(violation, 0) + 1
            )
    return summary


def summarise(records: list[RunRecord]) -> list[ArmSummary]:
    arms: dict[str, list[RunRecord]] = {}
    for record in records:
        arms.setdefault(record.arm, []).append(record)
    return [summarise_arm(arm, rows) for arm, rows in arms.items()]


# --- output ------------------------------------------------------------------


def write_runs(records: list[RunRecord], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    rows = [record.as_dict() for record in records]

    json_path = directory / "runs.json"
    json_path.write_text(json.dumps(rows, indent=2, default=str))

    csv_path = directory / "runs.csv"
    if rows:
        with csv_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return json_path, csv_path


def write_summary(summaries: list[ArmSummary], directory: Path, metadata: dict) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict

    payload = {"metadata": metadata, "arms": [asdict(summary) for summary in summaries]}
    json_path = directory / "summary.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    csv_path = directory / "summary.csv"
    rows = []
    for summary in summaries:
        row = asdict(summary)
        row["rejected_by_stage"] = ";".join(
            f"{stage}={count}" for stage, count in sorted(row["rejected_by_stage"].items())
        )
        row["violations_by_property"] = ";".join(
            f"{name}={count}" for name, count in sorted(row["violations_by_property"].items())
        )
        rows.append(row)
    if rows:
        with csv_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return json_path, csv_path


# --- the printed table -------------------------------------------------------

HEADLINE = (
    ("proposals", "proposals", "{}"),
    ("not_expressible", "n/expr", "{}"),
    ("unsafe_acceptance_rate", "unsafe acc", "{}"),
    ("unsafe_outcome_rate", "unsafe out", "{}"),
    ("false_rejection_rate", "false rej", "{}"),
    ("sound_adaptation_success_rate", "sound adapt ok", "{}"),
    ("cooling_effectiveness_rate", "cooled when deployed", "{}"),
    ("max_peak_temp_c", "max peak", "{}C"),
    ("forbidden_pin_writes", "fp writes", "{}"),
    ("rollback_success_rate", "rollback", "{}"),
    ("offline_lifecycle_completion_rate", "offline ok", "{}"),
    ("stale_update_rejection_rate", "stale rej", "{}"),
)


def render_table(summaries: list[ArmSummary]) -> str:
    lines = []
    width = max(len(summary.arm) for summary in summaries) + 2
    header = "metric".ljust(36) + "".join(summary.arm.ljust(width) for summary in summaries)
    lines.append(header)
    lines.append("-" * len(header))
    for attribute, label, template in HEADLINE:
        cells = []
        for summary in summaries:
            value = getattr(summary, attribute)
            cells.append(("n/a" if value is None else template.format(value)).ljust(width))
        lines.append(label.ljust(36) + "".join(cells))
    return "\n".join(lines)

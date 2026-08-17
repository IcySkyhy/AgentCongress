"""Small, dependency-free aggregation for benchmark run manifests.

The module deliberately separates operational counts from quality comparisons:
all manifests contribute to their ``task_id + condition`` group, while a paired
quality comparison requires both runs to be completed, validation-passing, and
to contain a finite numeric score.

Normalized quality is 1 at parity and larger when the comparison is better::

    1 + direction_adjusted(plan_score - single_score)
        / max(abs(single_score), abs(plan_score))

This relative-difference form remains defined when a legitimate score is zero.
Cost and time are ordinary plan/single ratios, so values below 1 are cheaper or
faster.  A zero single-arm cost or duration is treated as missing rather than as
an infinite result.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping


ScoreDirection = Literal["lower", "higher"]
ManifestSource = Path | str | Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ScoreConfig:
    """How to read and compare a task's score.

    ``field`` is relative to the manifest's score object (normally
    ``outcome.score``).  Manifests using the portable ``score.value`` shape do
    not need to set it.
    """

    direction: ScoreDirection
    field: str = "value"
    tie_tolerance: float = 0.0

    def __post_init__(self) -> None:
        if self.direction not in {"lower", "higher"}:
            raise ValueError("score direction must be 'lower' or 'higher'")
        if not self.field:
            raise ValueError("score field must not be empty")
        if self.tie_tolerance < 0 or not math.isfinite(self.tie_tolerance):
            raise ValueError("tie tolerance must be a finite non-negative number")


@dataclass(frozen=True, slots=True)
class GroupSummary:
    task_id: str
    condition: str
    n: int
    completed: int
    infrastructure_failures: int
    validation_passes: int
    numeric_scores: int
    score_direction: ScoreDirection | None
    exploratory: bool


@dataclass(frozen=True, slots=True)
class PairSummary:
    baseline_condition: str
    comparison_condition: str
    matched_pairs: int
    n: int
    independent_tasks: int
    wins: int
    ties: int
    losses: int
    median_normalized_score: float | None
    median_normalized_cost: float | None
    median_normalized_time: float | None
    exploratory: bool


@dataclass(frozen=True, slots=True)
class ExperimentAnalysis:
    groups: tuple[GroupSummary, ...]
    comparison: PairSummary

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Run:
    manifest: Mapping[str, Any]
    task_id: str
    condition: str
    status: str
    validation_passed: bool
    infrastructure_failure: bool
    scorer_failure: bool
    score: float | None
    direction: ScoreDirection | None
    tie_tolerance: float
    cost: float | None
    elapsed: float | None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _load(source: ManifestSource) -> Mapping[str, Any]:
    if isinstance(source, Mapping):
        return source
    raw = json.loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"manifest must contain a JSON object: {source}")
    return raw


def _score_block(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    outcome = manifest.get("outcome")
    if isinstance(outcome, Mapping) and isinstance(outcome.get("score"), Mapping):
        return outcome["score"]
    score = manifest.get("score")
    return score if isinstance(score, Mapping) else {}


def _embedded_score_config(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    value = manifest.get("score_config")
    return value if isinstance(value, Mapping) else {}


def _coerce_config(value: ScoreConfig | Mapping[str, Any] | str) -> ScoreConfig:
    if isinstance(value, ScoreConfig):
        return value
    if isinstance(value, str):
        return ScoreConfig(direction=value)  # type: ignore[arg-type]
    return ScoreConfig(
        direction=str(value["direction"]),  # type: ignore[arg-type]
        field=str(value.get("field", "value")),
        tie_tolerance=float(value.get("tie_tolerance", 0.0)),
    )


def _declared_direction(manifest: Mapping[str, Any]) -> ScoreDirection | None:
    candidates = (
        _score_block(manifest).get("direction"),
        manifest.get("score_direction"),
        _embedded_score_config(manifest).get("direction"),
    )
    for value in candidates:
        if value is None:
            continue
        if value not in {"lower", "higher"}:
            raise ValueError(f"invalid score direction for task {manifest.get('task_id')!r}: {value!r}")
        return value  # type: ignore[return-value]
    return None


def _validation_passed(manifest: Mapping[str, Any]) -> bool:
    outcome = manifest.get("outcome")
    validation = outcome.get("validation") if isinstance(outcome, Mapping) else None
    if not isinstance(validation, Mapping):
        validation = manifest.get("validation")
    return isinstance(validation, Mapping) and validation.get("passed") is True


def _infrastructure_failure(manifest: Mapping[str, Any], validation_passed: bool) -> bool:
    outcome = manifest.get("outcome")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    execution_status = outcome.get("execution_status", manifest.get("execution_status"))
    if isinstance(execution_status, str):
        # A typed execution outcome takes precedence over the legacy heuristic
        # below.  In particular, protocol, human-input, validation, and budget
        # failures are intention-to-treat outcomes, not rerunnable infra.
        return execution_status.casefold().replace("-", "_") == "infra_error"
    explicit = outcome.get("infrastructure_failure", manifest.get("infrastructure_failure"))
    if isinstance(explicit, bool):
        return explicit
    failure_kind = outcome.get("failure_kind", manifest.get("failure_kind"))
    if isinstance(failure_kind, str):
        return failure_kind.casefold() in {"infra", "infrastructure"}
    status = str(manifest.get("status", "")).casefold().replace("-", "_")
    if status in {"infra_failed", "infrastructure_failed", "infrastructure_failure"}:
        return True
    # Older manifests did not carry failure_kind.  A terminal error before any
    # validation result is an operationally unscorable run; budget exhaustion
    # remains a separately observable outcome rather than an infra failure.
    return status == "failed" and bool(outcome.get("error")) and not validation_passed and "validation" not in outcome


def _scorer_failure(manifest: Mapping[str, Any]) -> bool:
    outcome = manifest.get("outcome")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    execution_status = outcome.get("execution_status", manifest.get("execution_status"))
    return (
        isinstance(execution_status, str)
        and execution_status.casefold().replace("-", "_") == "scorer_error"
    )


def _value_at_field(block: Mapping[str, Any], field: str) -> Any:
    value: Any = block
    for part in field.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _score_value(manifest: Mapping[str, Any], config: ScoreConfig | None) -> float | None:
    block = _score_block(manifest)
    if block.get("valid") is False:
        return None
    field = config.field if config is not None else str(_embedded_score_config(manifest).get("field", "value"))
    value = _finite_number(_value_at_field(block, field))
    if value is not None:
        return value
    # Compatibility with the first AgentCongress performance manifests.  It is
    # intentionally narrow: return codes and arbitrary numeric metadata must
    # never be mistaken for benchmark quality.
    if field == "value":
        return _finite_number(block.get("cycles"))
    return None


def _metric(manifest: Mapping[str, Any], key: str) -> float | None:
    budget = manifest.get("budget")
    if not isinstance(budget, Mapping):
        return None
    return _finite_number(budget.get(key))


def _pair_identity(run: _Run) -> tuple[Any, ...]:
    manifest = run.manifest
    outcome = manifest.get("outcome")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    explicit = next(
        (
            value
            for key in ("pair_id", "replicate_id", "trial_id", "seed")
            if (value := manifest.get(key, outcome.get(key))) is not None
        ),
        None,
    )
    if explicit is None:
        raise ValueError(
            f"formal paired analysis requires pair_id for run {manifest.get('run_id')!r}"
        )
    return (
        run.task_id,
        manifest.get("reasoning_effort"),
        manifest.get("task_config_sha256"),
        manifest.get("repository_revision"),
        manifest.get("harness_tree_sha256"),
        explicit,
    )


def _sort_key(run: _Run) -> tuple[float, str]:
    created = _finite_number(run.manifest.get("created_at"))
    return (created if created is not None else 0.0, str(run.manifest.get("run_id", "")))


def _normalized_quality(single: float, comparison: float, direction: ScoreDirection) -> float:
    scale = max(abs(single), abs(comparison))
    if scale == 0:
        return 1.0
    improvement = single - comparison if direction == "lower" else comparison - single
    return 1.0 + improvement / scale


def _ratio(single: float | None, comparison: float | None) -> float | None:
    if single is None or comparison is None or single <= 0 or comparison < 0:
        return None
    return comparison / single


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def analyze_manifests(
    manifests: Iterable[ManifestSource],
    *,
    score_configs: Mapping[str, ScoreConfig | Mapping[str, Any] | str] | None = None,
    baseline_condition: str = "single",
    comparison_condition: str = "plan-execute",
) -> ExperimentAnalysis:
    """Aggregate manifests and compare any explicitly named paired conditions.

    A score direction may be frozen into a manifest (``score.direction``,
    ``score_direction``, or ``score_config.direction``) or supplied per task in
    ``score_configs``.  Conflicting declarations raise instead of silently
    reversing a result.
    """

    score_configs = score_configs or {}
    raw_manifests = [_load(source) for source in manifests]
    task_configs = {task_id: _coerce_config(value) for task_id, value in score_configs.items()}

    directions: dict[str, ScoreDirection | None] = {}
    for manifest in raw_manifests:
        task_id = str(manifest.get("task_id", ""))
        if not task_id:
            raise ValueError("manifest is missing task_id")
        declared = _declared_direction(manifest)
        configured = task_configs.get(task_id)
        candidates = {value for value in (declared, configured.direction if configured else None) if value is not None}
        previous = directions.get(task_id)
        if previous is not None:
            candidates.add(previous)
        if len(candidates) > 1:
            raise ValueError(f"conflicting score directions for task {task_id!r}: {sorted(candidates)}")
        directions[task_id] = next(iter(candidates), None)

    runs: list[_Run] = []
    for manifest in raw_manifests:
        task_id = str(manifest["task_id"])
        condition = str(manifest.get("condition", ""))
        if not condition:
            raise ValueError(f"manifest for task {task_id!r} is missing condition")
        validation_passed = _validation_passed(manifest)
        config = task_configs.get(task_id)
        embedded = _embedded_score_config(manifest)
        tolerance = config.tie_tolerance if config is not None else float(embedded.get("tie_tolerance", _score_block(manifest).get("tie_tolerance", 0.0)))
        if tolerance < 0 or not math.isfinite(tolerance):
            raise ValueError(f"invalid tie tolerance for task {task_id!r}")
        runs.append(
            _Run(
                manifest=manifest,
                task_id=task_id,
                condition=condition,
                status=str(manifest.get("status", "")),
                validation_passed=validation_passed,
                infrastructure_failure=_infrastructure_failure(manifest, validation_passed),
                scorer_failure=_scorer_failure(manifest),
                score=_score_value(manifest, config),
                direction=directions[task_id],
                tie_tolerance=tolerance,
                cost=_metric(manifest, "estimated_api_equivalent_cost_usd"),
                elapsed=_metric(manifest, "elapsed_seconds"),
            )
        )

    grouped: dict[tuple[str, str], list[_Run]] = {}
    for run in runs:
        grouped.setdefault((run.task_id, run.condition), []).append(run)
    groups = tuple(
        GroupSummary(
            task_id=task_id,
            condition=condition,
            n=len(group),
            completed=sum(run.status == "completed" for run in group),
            infrastructure_failures=sum(run.infrastructure_failure for run in group),
            validation_passes=sum(run.validation_passed for run in group),
            numeric_scores=sum(run.score is not None for run in group),
            score_direction=directions[task_id],
            exploratory=len(group) < 3,
        )
        for (task_id, condition), group in sorted(grouped.items())
    )

    # A replicate is one multi-arm block.  Infra/scorer failure in any arm
    # invalidates every quality contrast from that task/pair_id block, while
    # all runs remain present in the operational group summaries above.
    block_identities = {id(run): _pair_identity(run) for run in runs}
    invalid_blocks = {
        block_identities[id(run)]
        for run in runs
        if run.infrastructure_failure or run.scorer_failure
    }

    arms: dict[tuple[Any, ...], dict[str, list[_Run]]] = {}
    for run in runs:
        if run.condition in {baseline_condition, comparison_condition}:
            arms.setdefault(block_identities[id(run)], {}).setdefault(run.condition, []).append(run)

    matched: list[tuple[_Run, _Run]] = []
    for block_identity, arm in arms.items():
        if block_identity in invalid_blocks:
            continue
        singles = sorted(arm.get(baseline_condition, []), key=_sort_key)
        comparisons = sorted(arm.get(comparison_condition, []), key=_sort_key)
        matched.extend(zip(singles, comparisons, strict=False))

    scored: list[tuple[_Run, _Run]] = []
    normalized_scores: list[float] = []
    normalized_costs: list[float] = []
    normalized_times: list[float] = []
    wins = ties = losses = 0
    for single, comparison in matched:
        if not (
            single.status == comparison.status == "completed"
            and single.validation_passed
            and comparison.validation_passed
            and single.score is not None
            and comparison.score is not None
            and single.direction is not None
            and single.direction == comparison.direction
        ):
            continue
        scored.append((single, comparison))
        tolerance = max(single.tie_tolerance, comparison.tie_tolerance)
        if math.isclose(single.score, comparison.score, rel_tol=tolerance, abs_tol=tolerance):
            ties += 1
        else:
            comparison_won = comparison.score < single.score if single.direction == "lower" else comparison.score > single.score
            if comparison_won:
                wins += 1
            else:
                losses += 1
        normalized_scores.append(_normalized_quality(single.score, comparison.score, single.direction))
        if (ratio := _ratio(single.cost, comparison.cost)) is not None:
            normalized_costs.append(ratio)
        if (ratio := _ratio(single.elapsed, comparison.elapsed)) is not None:
            normalized_times.append(ratio)

    independent_tasks = len(
        {single.task_id for single, _comparison in scored}
    )
    comparison = PairSummary(
        baseline_condition=baseline_condition,
        comparison_condition=comparison_condition,
        matched_pairs=len(matched),
        n=len(scored),
        independent_tasks=independent_tasks,
        wins=wins,
        ties=ties,
        losses=losses,
        median_normalized_score=_median(normalized_scores),
        median_normalized_cost=_median(normalized_costs),
        median_normalized_time=_median(normalized_times),
        # Repetitions estimate within-task variability; they do not create
        # additional independent benchmark tasks.
        exploratory=independent_tasks < 3,
    )
    return ExperimentAnalysis(groups=groups, comparison=comparison)


def condition_for(strategy: str, planner_model: str, executor_model: str) -> str:
    if strategy not in {"self", "congress"}:
        raise ValueError("strategy must be self or congress")
    return f"{strategy}-v1-{planner_model.replace('.', '_')}-to-{executor_model.replace('.', '_')}"

from __future__ import annotations

import json

import pytest

from agentcongress.analysis import ScoreConfig, analyze_manifests, condition_for


def manifest(
    task: str,
    condition: str,
    score: float | None,
    *,
    direction: str | None = None,
    pair_id: str = "trial-1",
    status: str = "completed",
    validation: bool | None = True,
    cost: float = 1.0,
    elapsed: float = 10.0,
    error: str | None = None,
) -> dict:
    score_payload = {} if score is None else {"value": score}
    if direction is not None:
        score_payload["direction"] = direction
    outcome: dict = {"score": score_payload}
    if validation is not None:
        outcome["validation"] = {"passed": validation}
    if error is not None:
        outcome["error"] = error
    return {
        "run_id": f"{task}-{condition}-{pair_id}",
        "pair_id": pair_id,
        "task_id": task,
        "condition": condition,
        "status": status,
        "model": "test-model",
        "reasoning_effort": "high",
        "task_config_sha256": f"config-{task}",
        "repository_revision": f"repo-{task}",
        "harness_tree_sha256": "harness",
        "outcome": outcome,
        "budget": {
            "estimated_api_equivalent_cost_usd": cost,
            "elapsed_seconds": elapsed,
        },
    }


def test_groups_count_outcomes_and_mark_small_samples_exploratory() -> None:
    runs = [
        manifest("task", "single", 10, pair_id="1"),
        manifest("task", "single", 11, pair_id="2", status="failed", validation=None, error="worker timed out"),
        manifest("task", "single", 12, pair_id="3", status="failed", validation=False),
        manifest("other", "single", 1, direction="higher"),
    ]

    result = analyze_manifests(runs, score_configs={"task": ScoreConfig("lower")})
    group = next(group for group in result.groups if (group.task_id, group.condition) == ("task", "single"))

    assert group.n == 3
    assert group.completed == 1
    assert group.infrastructure_failures == 1
    assert group.validation_passes == 1
    assert group.numeric_scores == 3
    assert group.score_direction == "lower"
    assert group.exploratory is False
    assert next(group for group in result.groups if group.task_id == "other").exploratory is True


def test_pair_summary_is_direction_aware_and_normalized_across_tasks() -> None:
    runs = [
        manifest("lower-win", "single", 100, pair_id="a", cost=2, elapsed=10),
        manifest("lower-win", "plan-execute", 80, direction="lower", pair_id="a", cost=3, elapsed=12),
        manifest("higher-tie", "single", 0.5, pair_id="b", cost=4, elapsed=20),
        manifest("higher-tie", "plan-execute", 0.5001, pair_id="b", cost=4, elapsed=10),
        manifest("higher-loss", "single", 2, direction="higher", pair_id="c", cost=2, elapsed=8),
        manifest("higher-loss", "plan-execute", 1, direction="higher", pair_id="c", cost=1, elapsed=16),
    ]
    configs = {
        "lower-win": {"direction": "lower"},
        "higher-tie": ScoreConfig("higher", tie_tolerance=0.001),
    }

    summary = analyze_manifests(runs, score_configs=configs).comparison

    assert summary.matched_pairs == 3
    assert summary.n == 3
    assert summary.independent_tasks == 3
    assert (summary.wins, summary.ties, summary.losses) == (1, 1, 1)
    # Per-pair normalized qualities are 1.2, ~1.0002, and 0.5.
    assert summary.median_normalized_score == pytest.approx(1.0001999600079985)
    assert summary.median_normalized_cost == pytest.approx(1.0)
    assert summary.median_normalized_time == pytest.approx(1.2)
    assert summary.exploratory is False


def test_repetitions_do_not_inflate_independent_task_count() -> None:
    runs = []
    for pair_id in ("r1", "r2", "r3"):
        runs.extend(
            [
                manifest("one-task", "single", 10, direction="lower", pair_id=pair_id),
                manifest(
                    "one-task",
                    "plan-execute",
                    8,
                    direction="lower",
                    pair_id=pair_id,
                ),
            ]
        )

    summary = analyze_manifests(runs).comparison

    assert summary.n == 3
    assert summary.independent_tasks == 1
    assert summary.exploratory is True


def test_invalid_runs_are_matched_but_excluded_from_quality_statistics() -> None:
    runs = [
        manifest("task", "single", 10),
        manifest("task", "plan-execute", 5, status="failed", validation=False),
    ]

    summary = analyze_manifests(runs, score_configs={"task": "lower"}).comparison

    assert summary.matched_pairs == 1
    assert summary.n == 0
    assert (summary.wins, summary.ties, summary.losses) == (0, 0, 0)
    assert summary.median_normalized_score is None
    assert summary.exploratory is True


def test_loads_paths_supports_legacy_cycles_and_rejects_direction_conflicts(tmp_path) -> None:
    single = manifest("task", "single", None)
    single["outcome"]["score"] = {"cycles": 100, "direction": "lower"}
    planned = manifest("task", "plan-execute", None)
    planned["outcome"]["score"] = {"cycles": 90, "direction": "lower"}
    paths = []
    for index, value in enumerate((single, planned)):
        path = tmp_path / f"{index}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(path)

    result = analyze_manifests(paths)
    assert result.comparison.wins == 1

    planned["outcome"]["score"]["direction"] = "higher"
    with pytest.raises(ValueError, match="conflicting score directions"):
        analyze_manifests([single, planned])


def test_explicitly_invalid_structured_score_is_not_quality_data() -> None:
    run = manifest("task", "single", 1, direction="lower")
    run["outcome"]["score"]["valid"] = False
    result = analyze_manifests([run])
    assert result.groups[0].numeric_scores == 0


def test_new_protocol_conditions_can_be_paired_explicitly() -> None:
    self_condition = condition_for("self", "gpt-5.6-luna", "gpt-5.6-luna")
    congress_condition = condition_for("congress", "gpt-5.6-luna", "gpt-5.6-luna")
    runs = [
        manifest("task", self_condition, 10, direction="lower"),
        manifest("task", congress_condition, 8, direction="lower"),
    ]
    summary = analyze_manifests(runs, baseline_condition=self_condition, comparison_condition=congress_condition).comparison
    assert summary.matched_pairs == 1
    assert summary.wins == 1


def test_formal_pairing_requires_pair_id_and_ignores_executor_model() -> None:
    single = manifest("task", "single", 10, direction="lower")
    comparison = manifest("task", "plan-execute", 8, direction="lower")
    single["model"] = "gpt-5.6-luna"
    comparison["model"] = "gpt-5.6-sol"
    summary = analyze_manifests([single, comparison]).comparison
    assert summary.matched_pairs == 1

    single.pop("pair_id")
    with pytest.raises(ValueError, match="requires pair_id"):
        analyze_manifests([single, comparison])


def test_infrastructure_failure_invalidates_the_entire_pair() -> None:
    single = manifest("task", "single", 10, direction="lower")
    comparison = manifest("task", "plan-execute", 8, direction="lower")
    comparison["status"] = "failed"
    comparison["outcome"].update(
        execution_status="infra_error", infrastructure_failure=True
    )

    summary = analyze_manifests([single, comparison]).comparison
    assert summary.matched_pairs == 0
    assert summary.n == 0


def test_typed_infra_error_wins_over_legacy_false_default() -> None:
    single = manifest("task", "single", 10, direction="lower")
    comparison = manifest("task", "plan-execute", 8, direction="lower")
    comparison["outcome"].update(
        execution_status="infra_error", infrastructure_failure=False
    )

    result = analyze_manifests([single, comparison])

    assert result.comparison.matched_pairs == 0
    failed_group = next(
        group for group in result.groups if group.condition == "plan-execute"
    )
    assert failed_group.infrastructure_failures == 1


@pytest.mark.parametrize("failure_status", ["infra_error", "scorer_error"])
def test_third_arm_infra_or_scorer_failure_invalidates_quality_contrast(
    failure_status: str,
) -> None:
    single = manifest("task", "single", 10, direction="lower")
    comparison = manifest("task", "plan-execute", 8, direction="lower")
    third_arm = manifest("task", "third-arm", None, status="failed")
    third_arm["outcome"]["execution_status"] = failure_status
    if failure_status == "infra_error":
        third_arm["outcome"]["infrastructure_failure"] = True

    result = analyze_manifests([single, comparison, third_arm])

    assert result.comparison.matched_pairs == 0
    assert result.comparison.n == 0
    assert {(group.condition, group.n) for group in result.groups} == {
        ("single", 1),
        ("plan-execute", 1),
        ("third-arm", 1),
    }


def test_third_arm_protocol_failure_does_not_invalidate_quality_contrast() -> None:
    single = manifest("task", "single", 10, direction="lower")
    comparison = manifest("task", "plan-execute", 8, direction="lower")
    third_arm = manifest(
        "task",
        "third-arm",
        None,
        status="failed",
        validation=None,
        error="critic report was invalid",
    )
    third_arm["outcome"]["execution_status"] = "protocol_failure"

    result = analyze_manifests([single, comparison, third_arm])

    assert result.comparison.matched_pairs == 1
    assert result.comparison.n == 1
    assert result.comparison.wins == 1
    third_group = next(group for group in result.groups if group.condition == "third-arm")
    assert third_group.infrastructure_failures == 0

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from agentcongress.stage_two import (
    StageTwoValidationError,
    build_stage_two_plan,
    load_stage_two_environment_lock,
    load_stage_two_suite,
)


SUITE_PATH = Path("examples/benchmarks/stage-two-suite.yaml")


def _write_suite(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load(SUITE_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _ready_suite(tmp_path: Path):
    def ready(raw):
        raw["suite"].update(environment_frozen=True, readiness="ready")
        for index, task in enumerate(raw["tasks"], 1):
            task["image"]["digest"] = f"sha256:{index:064x}"

    return load_stage_two_suite(_write_suite(tmp_path, ready))


def _write_environment_lock(tmp_path: Path, suite, mutate=lambda lock: None) -> Path:
    evidence_root = tmp_path / "evidence"
    files: dict[str, bytes] = {"backend/isolation.json": b'{"measured":true}\n'}
    task_rows = []
    digests = {task["id"]: task["image"]["digest"] for task in suite.raw["tasks"]}
    for task_id in suite.task_ids:
        artifacts = {}
        for kind in ("task_metadata", "verifier", "oracle", "noop"):
            relative = f"tasks/{task_id}/{kind}.json"
            files[relative] = f"{task_id}:{kind}\n".encode()
            artifacts[kind] = {
                "path": relative,
                "sha256": hashlib.sha256(files[relative]).hexdigest(),
            }
        task_rows.append({"id": task_id, "image_digest": digests[task_id], "artifacts": artifacts})
    for relative, payload in files.items():
        artifact = evidence_root / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(payload)
    lock = {
        "schema_version": 1,
        "suite_id": suite.suite_id,
        "suite_sha256": suite.suite_sha256,
        "evidence_level": "measured",
        "evidence_root": "evidence",
        "backend": {
            "id": "harbor-docker",
            "version": "0.20.0",
            "runtime": "docker",
            "runtime_version": "29.0.0",
            "evidence": {
                "path": "backend/isolation.json",
                "sha256": hashlib.sha256(files["backend/isolation.json"]).hexdigest(),
            },
        },
        "tasks": task_rows,
    }
    mutate(lock)
    path = tmp_path / "environment.lock.json"
    path.write_text(json.dumps(lock, sort_keys=True), encoding="utf-8")
    return path


def test_current_suite_is_valid_but_fails_closed_with_specific_blockers() -> None:
    suite = load_stage_two_suite(SUITE_PATH)
    assert suite.suite_id == "stage-two-v1"
    assert suite.suite_sha256 == hashlib.sha256(SUITE_PATH.read_bytes()).hexdigest()
    assert list(suite.task_ids) == [
        "django__django-12143",
        "django__django-12273",
        "fix-ocaml-gc",
        "db-wal-recovery",
        "fix-code-vulnerability",
    ]
    assert list(suite.condition_ids) == list("ABCDE")

    blockers = suite.readiness_blockers
    assert suite.ready is False
    assert [blocker.code for blocker in blockers].count("environment_not_frozen") == 1
    assert [blocker.code for blocker in blockers].count("declared_readiness_blocked") == 1
    assert [blocker.code for blocker in blockers].count("image_digest_missing") == 0
    assert [blocker.code for blocker in blockers].count("execution_backend_missing") == 1

    plan = build_stage_two_plan(suite, "pilot")
    assert plan.ready is False
    assert plan.blockers == blockers
    assert len(plan.blocks) == 5
    assert all(set(block.realized_order) == set("ABCDE") for block in plan.blocks)
    assert all(len(block.arms) == 5 for block in plan.blocks)


def test_confirmatory_plan_is_deterministic_and_freezes_each_complete_block() -> None:
    suite = load_stage_two_suite(SUITE_PATH)
    first = build_stage_two_plan(suite, "confirmatory")
    second = build_stage_two_plan(suite, "confirmatory")
    assert first.as_dict() == second.as_dict()
    assert first.to_json() == second.to_json()
    assert len(first.blocks) == 10
    assert len({block.pair_id for block in first.blocks}) == 10
    assert len({block.block_seed for block in first.blocks}) == 10

    for block in first.blocks:
        assert block.pair_id.startswith("stage-two-v1:confirmatory:")
        assert block.replicate in {1, 2}
        assert block.base_seed == 20260812
        assert tuple(arm.condition_id for arm in block.arms) == block.realized_order
        assert [arm.order for arm in block.arms] == [1, 2, 3, 4, 5]
        assert set(block.realized_order) == set("ABCDE")
        assert block.budget["total_agent_seconds"] == 1200
        assert sum(slot["max_seconds"] for slot in block.budget["slots"]) == 1200
        arm_e = next(arm for arm in block.arms if arm.condition_id == "E")
        assert arm_e.config["analysis_model"] == "gpt-5.6-luna"
        assert arm_e.config["execution_model"] == "gpt-5.6-sol"
        assert block.task_config["source"]["revision"]
        assert block.task_config["task"]["scorer"]["success_value"] == 1.0


def test_flags_and_image_digests_alone_do_not_clear_readiness(tmp_path: Path) -> None:
    def ready(raw):
        raw["suite"]["environment_frozen"] = True
        raw["suite"]["readiness"] = "ready"
        for index, task in enumerate(raw["tasks"], 1):
            task["image"]["digest"] = f"sha256:{index:064x}"

    suite = load_stage_two_suite(_write_suite(tmp_path, ready))
    assert suite.ready is False
    plan = build_stage_two_plan(suite, "pilot")
    assert plan.ready is False
    assert {blocker.code for blocker in plan.blockers} == {"execution_backend_missing"}
    assert all(block.task_config["task"]["image"]["digest"].startswith("sha256:") for block in plan.blocks)


def test_inline_backend_and_oracle_hashes_cannot_clear_readiness(tmp_path: Path) -> None:
    def ready(raw):
        raw["suite"].update(environment_frozen=True, readiness="ready")
        for index, task in enumerate(raw["tasks"], 1):
            task["image"]["digest"] = f"sha256:{index:064x}"
        raw["execution_backend"] = {
            "implementation": "harbor",
            "version": "0.20.0",
            "container_runtime": "docker",
            "runtime_version": "29.0.0",
            "isolation_evidence_sha256": "a" * 64,
        }
        raw["oracle_evidence"] = {
            task["id"]: {name: format(index, "064x") for name in (
                "task_metadata_sha256", "verifier_sha256", "oracle_result_sha256", "no_op_result_sha256"
            )}
            for index, task in enumerate(raw["tasks"], 1)
        }

    suite = load_stage_two_suite(_write_suite(tmp_path, ready))
    assert suite.ready is False
    assert {blocker.code for blocker in build_stage_two_plan(suite, "pilot").blockers} == {"execution_backend_missing"}


def test_measured_environment_lock_clears_only_the_external_gate(tmp_path: Path) -> None:
    suite = _ready_suite(tmp_path)
    lock = load_stage_two_environment_lock(_write_environment_lock(tmp_path, suite), suite)

    assert lock.verified is True
    assert lock.backend_id == "harbor-docker"
    assert build_stage_two_plan(suite, "pilot", lock).blockers == ()
    assert build_stage_two_plan(suite, "pilot", lock).ready is True
    assert suite.ready is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda lock: lock.__setitem__("evidence_level", "simulated"), "must be measured"),
        (lambda lock: lock.__setitem__("suite_id", "wrong-suite"), "wrong suite"),
        (lambda lock: lock["tasks"].pop(), "exactly the frozen task ids"),
        (lambda lock: lock["tasks"][0].__setitem__("id", "wrong-task"), "exactly the frozen task ids"),
        (lambda lock: lock["backend"].__setitem__("id", "pretend-sealed"), "harbor-docker backend"),
        (lambda lock: lock["backend"].__setitem__("runtime", "host-shell"), "docker runtime"),
        (lambda lock: lock["tasks"][0].__setitem__("image_digest", "sha256:" + "f" * 64), "image digest"),
        (lambda lock: lock["backend"]["evidence"].__setitem__("sha256", "f" * 64), "sha256 does not match"),
        (lambda lock: lock["backend"]["evidence"].__setitem__("path", "../escape"), "normalized relative path"),
        (lambda lock: lock["backend"]["evidence"].__setitem__("path", str(Path.cwd().resolve() / "escape")), "normalized relative path"),
    ],
)
def test_environment_lock_rejects_invalid_bindings_and_artifacts(tmp_path: Path, mutate, message: str) -> None:
    suite = _ready_suite(tmp_path)
    with pytest.raises(StageTwoValidationError, match=message):
        load_stage_two_environment_lock(_write_environment_lock(tmp_path, suite, mutate), suite)


def test_environment_lock_rejects_missing_extra_tampered_and_symlink_evidence(tmp_path: Path) -> None:
    suite = _ready_suite(tmp_path)
    lock_path = _write_environment_lock(tmp_path, suite)
    artifact = tmp_path / "evidence" / "tasks" / suite.task_ids[0] / "oracle.json"
    artifact.write_bytes(b"tampered")
    with pytest.raises(StageTwoValidationError, match="sha256 does not match"):
        load_stage_two_environment_lock(lock_path, suite)

    lock_path = _write_environment_lock(tmp_path, suite)
    artifact.unlink()
    with pytest.raises(StageTwoValidationError, match="missing"):
        load_stage_two_environment_lock(lock_path, suite)

    lock_path = _write_environment_lock(tmp_path, suite)
    (tmp_path / "evidence" / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(StageTwoValidationError, match="managed evidence files"):
        load_stage_two_environment_lock(lock_path, suite)

    if hasattr(os, "symlink"):
        lock_path = _write_environment_lock(tmp_path, suite)
        artifact.unlink()
        try:
            artifact.symlink_to(tmp_path / "evidence" / "backend" / "isolation.json")
        except OSError:
            pytest.skip("symlink creation is not available")
        with pytest.raises(StageTwoValidationError, match="symlink"):
            load_stage_two_environment_lock(lock_path, suite)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw["tasks"].pop(), "exactly five"),
        (lambda raw: raw["conditions"].__setitem__(4, raw["conditions"][0]), "duplicate condition"),
        (lambda raw: raw["budget"]["slots"][2].__setitem__("max_seconds", 839), "three no-rollover slots"),
        (lambda raw: raw["tasks"][0]["image"].__setitem__("digest", "latest"), "immutable sha256 OCI digest"),
        (lambda raw: raw["suite"].__setitem__("environment_frozen", "false"), "must be a boolean"),
        (lambda raw: raw["suite"].pop("selection_frozen_at"), "selection_frozen_at"),
        (lambda raw: raw["oracle_gate"]["checks"].pop(), "oracle_gate.checks"),
    ],
)
def test_malformed_or_mutated_frozen_contract_is_rejected(tmp_path: Path, mutate, message: str) -> None:
    with pytest.raises(StageTwoValidationError, match=message):
        load_stage_two_suite(_write_suite(tmp_path, mutate))


def test_plan_rejects_unknown_phase() -> None:
    suite = load_stage_two_suite(SUITE_PATH)
    with pytest.raises(ValueError, match="phase"):
        build_stage_two_plan(suite, "exploratory")  # type: ignore[arg-type]

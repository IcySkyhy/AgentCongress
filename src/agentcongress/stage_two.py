from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml

Phase = Literal["pilot", "confirmatory"]

_SUITE_ID = "stage-two-v1"
_TASK_SOURCES = {
    "django__django-12143": "swe_bench_verified_mini",
    "django__django-12273": "swe_bench_verified_mini",
    "fix-ocaml-gc": "terminal_bench_2",
    "db-wal-recovery": "terminal_bench_2",
    "fix-code-vulnerability": "terminal_bench_2",
}
_SLOTS = (
    ("analysis", 180, "read_only"),
    ("critique", 180, "read_only"),
    ("execution", 840, "workspace_write"),
)
_CONDITIONS = {
    "A": ("LLL-self", "self", "gpt-5.6-luna", "gpt-5.6-luna", "same_as_analyst", "gpt-5.6-luna"),
    "B": ("LLL-congress", "congress", "gpt-5.6-luna", "gpt-5.6-luna", "independent_listener", "gpt-5.6-luna"),
    "C": ("SSS-self", "self", "gpt-5.6-sol", "gpt-5.6-sol", "same_as_analyst", "gpt-5.6-sol"),
    "D": ("LLS-self", "self", "gpt-5.6-luna", "gpt-5.6-luna", "same_as_analyst", "gpt-5.6-sol"),
    "E": ("LLS-congress", "congress", "gpt-5.6-luna", "gpt-5.6-luna", "independent_listener", "gpt-5.6-sol"),
}
_CONDITION_FIELDS = ("label", "strategy", "analysis_model", "critique_model", "critique_identity", "execution_model")
_CONTRASTS = (
    ("congress_same_model", "B-A"),
    ("congress_with_sol_executor", "E-D"),
    ("luna_deliberation_substitution", "D-C"),
    ("cheap_congress_leverage", "E-C"),
)
_ORACLE_CHECKS = (
    "source_revision_and_task_locator_resolve",
    "image_reference_resolves_to_recorded_immutable_digest",
    "verifier_files_and_solution_are_absent_during_agent_phase",
    "network_egress_probe_fails_during_agent_phase",
    "official_oracle_reaches_success_value_from_a_clean_environment",
    "no_op_control_does_not_reach_success_value",
    "verifier_output_is_machine_readable_and_archived",
)
_MANIFEST_FIELDS = {
    "framework_git_sha", "clean_framework_tree_or_archived_tree_hash", "codex_cli_version",
    "operating_system_and_architecture", "python_version", "container_runtime_version",
    "source_revision", "task_metadata_sha256", "image_digest", "verifier_sha256",
    "prompt_and_report_schema_sha256", "model_and_reasoning_effort_by_slot",
    "randomization_seed_and_realized_order", "usage_by_model", "wall_time_by_slot",
    "execution_status", "objective_success", "structured_score",
}
_GIT_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TASK_EVIDENCE_KINDS = ("task_metadata", "verifier", "oracle", "noop")
_SEALED_BACKEND_ID = "harbor-docker"
_SEALED_RUNTIME = "docker"
_VERIFIED_ENVIRONMENT_LOCK = object()


class StageTwoValidationError(ValueError):
    """The input is not the frozen five-task, five-arm Stage 2 contract."""


@dataclass(frozen=True, slots=True)
class ReadinessBlocker:
    code: str
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class StageTwoEnvironmentLock:
    """A content-verified binding between Stage 2 and measured environment evidence."""

    lock_path: Path
    lock_sha256: str
    suite_id: str
    suite_sha256: str
    evidence_root: Path
    backend_id: str
    backend_version: str
    runtime: str
    runtime_version: str
    task_ids: tuple[str, ...]
    task_image_digests: tuple[tuple[str, str], ...]
    artifact_sha256s: tuple[tuple[str, str], ...]
    _verification_token: object = field(repr=False, compare=False)

    @property
    def verified(self) -> bool:
        return self._verification_token is _VERIFIED_ENVIRONMENT_LOCK


@dataclass(frozen=True, slots=True)
class StageTwoSuite:
    suite_sha256: str
    suite_id: str
    task_ids: tuple[str, ...]
    condition_ids: tuple[str, ...]
    pilot_repetitions: int
    confirmatory_repetitions: int
    randomization_seed: int
    raw_json: str = field(repr=False)

    @property
    def raw(self) -> dict[str, Any]:
        return json.loads(self.raw_json)

    @property
    def readiness_blockers(self) -> tuple[ReadinessBlocker, ...]:
        return _readiness_blockers(self.raw, self.task_ids, self.suite_id)

    @property
    def ready(self) -> bool:
        return not self.readiness_blockers


@dataclass(frozen=True, slots=True)
class StageTwoArm:
    order: int
    condition_id: str
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StageTwoBlock:
    pair_id: str
    task_id: str
    replicate: int
    base_seed: int
    block_seed: int
    realized_order: tuple[str, ...]
    task_config: dict[str, Any]
    budget: dict[str, Any]
    arms: tuple[StageTwoArm, ...]


@dataclass(frozen=True, slots=True)
class StageTwoPlan:
    suite_id: str
    suite_sha256: str
    phase: Phase
    ready: bool
    blockers: tuple[ReadinessBlocker, ...]
    blocks: tuple[StageTwoBlock, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"


def load_stage_two_suite(path: Path) -> StageTwoSuite:
    """Strictly validate Stage 2 without acquiring tasks or calling a model."""

    payload = path.read_bytes()
    try:
        raw = _mapping(yaml.safe_load(payload.decode("utf-8")) or {}, "root")
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise StageTwoValidationError(f"invalid Stage 2 YAML: {exc}") from exc
    if _integer(_get(raw, "schema_version", "root"), "schema_version") != 1:
        raise StageTwoValidationError("schema_version must be 1")
    suite = _mapping(_get(raw, "suite", "root"), "suite")
    suite_id = _string(_get(suite, "id", "suite"), "suite.id")
    if suite_id != _SUITE_ID or suite.get("intended_claim") != "comparative_harness_effect":
        raise StageTwoValidationError("suite identity or intended claim has changed")
    try:
        date.fromisoformat(_string(_get(suite, "selection_frozen_at", "suite"), "suite.selection_frozen_at"))
    except ValueError as exc:
        raise StageTwoValidationError("suite.selection_frozen_at must be an ISO date") from exc
    _boolean(_get(suite, "protocol_frozen", "suite"), "suite.protocol_frozen")
    _boolean(_get(suite, "environment_frozen", "suite"), "suite.environment_frozen")
    _string(_get(suite, "readiness", "suite"), "suite.readiness")

    sources = _validate_sources(_get(raw, "sources", "root"))
    tasks = _validate_tasks(_get(raw, "tasks", "root"), sources)
    conditions = _validate_conditions(_get(raw, "conditions", "root"))
    _validate_budget(_get(raw, "budget", "root"))
    pilot, confirmatory, seed = _validate_design(raw)
    _validate_isolation(raw)
    return StageTwoSuite(
        hashlib.sha256(payload).hexdigest(), suite_id, tasks, conditions, pilot, confirmatory, seed,
        json.dumps(raw, sort_keys=True, separators=(",", ":")),
    )


def load_stage_two_environment_lock(
    path: Path,
    suite: StageTwoSuite,
) -> StageTwoEnvironmentLock:
    """Load a measured lock and verify every managed evidence byte."""

    lock_path = path.resolve()
    try:
        payload = lock_path.read_bytes()
        raw = _mapping(json.loads(payload.decode("utf-8")), "environment lock")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StageTwoValidationError(f"invalid Stage 2 environment lock: {exc}") from exc
    if set(raw) != {"schema_version", "suite_id", "suite_sha256", "evidence_level", "evidence_root", "backend", "tasks"}:
        raise StageTwoValidationError("environment lock fields are incomplete or contain unsupported entries")
    if _integer(raw["schema_version"], "environment lock.schema_version") != 1:
        raise StageTwoValidationError("environment lock.schema_version must be 1")
    if raw["suite_id"] != suite.suite_id or raw["suite_sha256"] != suite.suite_sha256:
        raise StageTwoValidationError("environment lock is bound to the wrong suite")
    if raw["evidence_level"] != "measured":
        raise StageTwoValidationError("environment lock evidence_level must be measured")

    root_relative = _safe_relative_path(raw["evidence_root"], "environment lock.evidence_root")
    root = lock_path.parent / root_relative
    if root.is_symlink():
        raise StageTwoValidationError("environment lock evidence_root must not be a symlink")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise StageTwoValidationError("environment lock evidence_root does not exist") from exc
    if not root.is_dir():
        raise StageTwoValidationError("environment lock evidence_root must be a directory")

    backend = _mapping(raw["backend"], "environment lock.backend")
    if set(backend) != {"id", "version", "runtime", "runtime_version", "evidence"}:
        raise StageTwoValidationError("environment lock.backend fields are incomplete or unsupported")
    backend_id = _string(backend["id"], "environment lock.backend.id")
    backend_version = _string(backend["version"], "environment lock.backend.version")
    runtime = _string(backend["runtime"], "environment lock.backend.runtime")
    runtime_version = _string(backend["runtime_version"], "environment lock.backend.runtime_version")
    if backend_id != _SEALED_BACKEND_ID or runtime != _SEALED_RUNTIME:
        raise StageTwoValidationError("environment lock must bind the harbor-docker backend and docker runtime")

    expected_artifacts: dict[str, str] = {}
    _verify_artifact_reference(backend["evidence"], "environment lock.backend.evidence", root, expected_artifacts)

    task_rows = _sequence(raw["tasks"], "environment lock.tasks")
    tasks = _indexed(task_rows, "environment lock.tasks")
    if set(tasks) != set(suite.task_ids) or len(task_rows) != len(suite.task_ids):
        raise StageTwoValidationError("environment lock must cover exactly the frozen task ids")
    suite_tasks = {task["id"]: task for task in suite.raw["tasks"]}
    image_digests: list[tuple[str, str]] = []
    for task_id in suite.task_ids:
        item = tasks[task_id]
        if set(item) != {"id", "image_digest", "artifacts"}:
            raise StageTwoValidationError(f"environment lock task {task_id!r} fields are incomplete or unsupported")
        image_digest = item["image_digest"]
        if image_digest != suite_tasks[task_id]["image"]["digest"] or not isinstance(image_digest, str) or not _DIGEST.fullmatch(image_digest):
            raise StageTwoValidationError(f"environment lock task {task_id!r} image digest does not match the suite")
        image_digests.append((task_id, image_digest))
        artifacts = _mapping(item["artifacts"], f"environment lock.tasks.{task_id}.artifacts")
        if set(artifacts) != set(_TASK_EVIDENCE_KINDS):
            raise StageTwoValidationError(f"environment lock task {task_id!r} must contain exactly the required evidence artifacts")
        for kind in _TASK_EVIDENCE_KINDS:
            _verify_artifact_reference(
                artifacts[kind],
                f"environment lock.tasks.{task_id}.artifacts.{kind}",
                root,
                expected_artifacts,
            )

    actual_artifacts: set[str] = set()
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise StageTwoValidationError("managed evidence must not contain symlinks")
        if candidate.is_file():
            actual_artifacts.add(candidate.relative_to(root).as_posix())
    if actual_artifacts != set(expected_artifacts):
        missing = sorted(set(expected_artifacts) - actual_artifacts)
        extra = sorted(actual_artifacts - set(expected_artifacts))
        raise StageTwoValidationError(f"managed evidence files do not match the lock (missing={missing}, extra={extra})")

    return StageTwoEnvironmentLock(
        lock_path=lock_path,
        lock_sha256=hashlib.sha256(payload).hexdigest(),
        suite_id=suite.suite_id,
        suite_sha256=suite.suite_sha256,
        evidence_root=root,
        backend_id=backend_id,
        backend_version=backend_version,
        runtime=runtime,
        runtime_version=runtime_version,
        task_ids=suite.task_ids,
        task_image_digests=tuple(image_digests),
        artifact_sha256s=tuple(sorted(expected_artifacts.items())),
        _verification_token=_VERIFIED_ENVIRONMENT_LOCK,
    )


def build_stage_two_plan(
    suite: StageTwoSuite,
    phase: Phase,
    environment_lock: StageTwoEnvironmentLock | None = None,
) -> StageTwoPlan:
    """Produce a deterministic audit plan even when readiness fails closed."""

    if phase not in {"pilot", "confirmatory"}:
        raise ValueError("phase must be 'pilot' or 'confirmatory'")
    raw = suite.raw
    repetitions = suite.pilot_repetitions if phase == "pilot" else suite.confirmatory_repetitions
    tasks = {item["id"]: item for item in raw["tasks"]}
    conditions = {item["id"]: item for item in raw["conditions"]}
    blocks: list[StageTwoBlock] = []
    for task_id in suite.task_ids:
        for replicate in range(1, repetitions + 1):
            material = f"{suite.suite_id}\0{phase}\0{task_id}\0{replicate}\0{suite.randomization_seed}".encode()
            block_seed = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
            order = list(suite.condition_ids)
            random.Random(block_seed).shuffle(order)
            task_config = {"task": tasks[task_id], "source": raw["sources"][tasks[task_id]["source"]]}
            arms = tuple(StageTwoArm(index, arm, conditions[arm]) for index, arm in enumerate(order, 1))
            blocks.append(StageTwoBlock(
                f"{suite.suite_id}:{phase}:{task_id}:r{replicate:02d}", task_id, replicate,
                suite.randomization_seed, block_seed, tuple(order), task_config, raw["budget"], arms,
            ))
    blockers = _readiness_blockers(suite.raw, suite.task_ids, suite.suite_id, environment_lock, suite.suite_sha256)
    return StageTwoPlan(suite.suite_id, suite.suite_sha256, phase, not blockers, blockers, tuple(blocks))


def _validate_sources(value: Any) -> dict[str, dict[str, Any]]:
    sources = _mapping(value, "sources")
    if set(sources) != set(_TASK_SOURCES.values()):
        raise StageTwoValidationError("sources must contain exactly the two frozen sources")
    for source_id, item in sources.items():
        source = _mapping(item, f"sources.{source_id}")
        if not _GIT_SHA.fullmatch(_string(_get(source, "revision", f"sources.{source_id}"), f"sources.{source_id}.revision")):
            raise StageTwoValidationError(f"sources.{source_id}.revision must be a full Git SHA")
    return sources


def _validate_tasks(value: Any, sources: dict[str, Any]) -> tuple[str, ...]:
    rows = _sequence(value, "tasks")
    indexed = _indexed(rows, "tasks")
    if len(rows) != 5 or set(indexed) != set(_TASK_SOURCES):
        raise StageTwoValidationError("tasks must contain exactly five frozen Stage 2 task ids")
    for task_id, source_id in _TASK_SOURCES.items():
        task = indexed[task_id]
        if task.get("source") != source_id or task.get("family") != source_id or source_id not in sources:
            raise StageTwoValidationError(f"tasks.{task_id} source/family has changed")
        _string(_get(task, "source_locator", f"tasks.{task_id}"), f"tasks.{task_id}.source_locator")
        image = _mapping(_get(task, "image", f"tasks.{task_id}"), f"tasks.{task_id}.image")
        if "digest" not in image:
            raise StageTwoValidationError(f"tasks.{task_id}.image.digest is required")
        digest = image["digest"]
        if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise StageTwoValidationError(f"tasks.{task_id}.image.digest must be null or an immutable sha256 OCI digest")
        _mapping(_get(task, "resources", f"tasks.{task_id}"), f"tasks.{task_id}.resources")
        _mapping(_get(task, "scorer", f"tasks.{task_id}"), f"tasks.{task_id}.scorer")
    return tuple(_TASK_SOURCES)


def _validate_conditions(value: Any) -> tuple[str, ...]:
    rows = _sequence(value, "conditions")
    indexed = _indexed(rows, "conditions")
    if len(rows) != 5 or set(indexed) != set(_CONDITIONS):
        raise StageTwoValidationError("conditions must contain exactly arms A-E")
    for arm, expected in _CONDITIONS.items():
        actual = tuple(indexed[arm].get(field) for field in _CONDITION_FIELDS)
        if actual != expected:
            raise StageTwoValidationError(f"conditions.{arm} does not match the frozen protocol")
    return tuple(_CONDITIONS)


def _validate_budget(value: Any) -> None:
    budget = _mapping(value, "budget")
    slots = tuple((item.get("name"), item.get("max_seconds"), item.get("filesystem")) for item in map(_mapping_slot, _sequence(_get(budget, "slots", "budget"), "budget.slots")))
    if (budget.get("worker_sessions"), budget.get("total_agent_seconds"), budget.get("no_rollover"), slots) != (3, 1200, True, _SLOTS):
        raise StageTwoValidationError("budget must freeze three no-rollover slots: analysis=180, critique=180, execution=840")
    if budget.get("reasoning_effort") != "high" or budget.get("validation_and_scoring") != "outside_agent_budget_with_task_specific_hard_timeout":
        raise StageTwoValidationError("budget effort or validation boundary has changed")


def _validate_design(raw: dict[str, Any]) -> tuple[int, int, int]:
    design = _mapping(_get(raw, "run_design", "root"), "run_design")
    pilot = _mapping(_get(design, "pilot", "run_design"), "run_design.pilot")
    confirm = _mapping(_get(design, "confirmatory", "run_design"), "run_design.confirmatory")
    randomization = _mapping(_get(design, "randomization", "run_design"), "run_design.randomization")
    repetitions = (pilot.get("repetitions_per_task_condition"), confirm.get("repetitions_per_task_condition"))
    third = (confirm.get("third_repetition_trigger"), confirm.get("third_repetition_scope"))
    if repetitions != (1, 2) or third != ("discordant_or_predeclared_near_threshold_result", "all_five_conditions_for_that_task"):
        raise StageTwoValidationError("run_design repetitions have changed")
    if randomization.get("unit") != "condition_order_within_task_and_repetition" or design.get("pairing_unit") != "task_and_repetition":
        raise StageTwoValidationError("run_design pairing/randomization has changed")
    contrasts = tuple((item.get("id"), item.get("expression")) for item in map(_mapping_slot, _sequence(_get(raw, "predeclared_contrasts", "root"), "predeclared_contrasts")))
    if contrasts != _CONTRASTS:
        raise StageTwoValidationError("predeclared_contrasts have changed")
    seed = _integer(_get(randomization, "seed", "run_design.randomization"), "run_design.randomization.seed")
    return 1, 2, seed


def _validate_isolation(raw: dict[str, Any]) -> None:
    isolation = _mapping(_get(raw, "isolation", "root"), "isolation")
    if (isolation.get("network_during_agent"), isolation.get("network_during_verifier"), isolation.get("verifier_mount")) != ("disabled", "disabled", "after_agent_exit"):
        raise StageTwoValidationError("isolation must disable both networks and mount the verifier after agent exit")
    if set(isolation.get("agent_forbidden", ())) < {"source_solution_directory", "source_tests_directory", "verifier_files", "gold_patch"}:
        raise StageTwoValidationError("isolation.agent_forbidden is incomplete")
    oracle = _mapping(_get(raw, "oracle_gate", "root"), "oracle_gate")
    if oracle.get("required_before_any_model_run") is not True or tuple(oracle.get("checks", ())) != _ORACLE_CHECKS:
        raise StageTwoValidationError("oracle_gate.checks do not match the frozen pre-model gate")
    fields = set(_sequence(_get(raw, "run_manifest_requirements", "root"), "run_manifest_requirements"))
    if not _MANIFEST_FIELDS.issubset(fields):
        raise StageTwoValidationError("run_manifest_requirements is incomplete")


def _readiness_blockers(
    raw: dict[str, Any],
    task_ids: tuple[str, ...],
    suite_id: str,
    environment_lock: StageTwoEnvironmentLock | None = None,
    suite_sha256: str | None = None,
) -> tuple[ReadinessBlocker, ...]:
    suite = raw["suite"]
    blockers: list[ReadinessBlocker] = []
    for condition, code, detail in (
        (not suite["protocol_frozen"], "protocol_not_frozen", "suite.protocol_frozen is false"),
        (not suite["environment_frozen"], "environment_not_frozen", "suite.environment_frozen is false"),
        (suite["readiness"] != "ready", "declared_readiness_blocked", f"suite.readiness is {suite['readiness']!r}"),
    ):
        if condition:
            blockers.append(ReadinessBlocker(code, suite_id, detail))
    tasks = {task["id"]: task for task in raw["tasks"]}
    blockers.extend(ReadinessBlocker("image_digest_missing", task_id, "task image digest is null") for task_id in task_ids if tasks[task_id]["image"]["digest"] is None)
    lock_matches = (
        environment_lock is not None
        and environment_lock.verified
        and environment_lock.suite_id == suite_id
        and environment_lock.suite_sha256 == suite_sha256
        and environment_lock.task_ids == task_ids
        and dict(environment_lock.task_image_digests)
        == {task_id: tasks[task_id]["image"]["digest"] for task_id in task_ids}
    )
    if not lock_matches:
        blockers.append(ReadinessBlocker("execution_backend_missing", suite_id, "no verified measured Stage 2 environment lock covers this suite"))
    return tuple(blockers)


def _safe_relative_path(value: Any, path: str) -> Path:
    text = _string(value, path)
    candidate = Path(text)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise StageTwoValidationError(f"{path} must be a normalized relative path without '..'")
    return candidate


def _verify_artifact_reference(
    value: Any,
    path: str,
    root: Path,
    expected_artifacts: dict[str, str],
) -> None:
    reference = _mapping(value, path)
    if set(reference) != {"path", "sha256"} or not _hash(reference.get("sha256")):
        raise StageTwoValidationError(f"{path} must contain only a relative path and sha256")
    relative = _safe_relative_path(reference["path"], f"{path}.path")
    relative_text = relative.as_posix()
    if relative_text in expected_artifacts:
        raise StageTwoValidationError(f"duplicate managed evidence path {relative_text!r}")
    artifact = root / relative
    if artifact.is_symlink():
        raise StageTwoValidationError(f"{path} must not reference a symlink")
    try:
        resolved = artifact.resolve(strict=True)
    except OSError as exc:
        raise StageTwoValidationError(f"{path} evidence file is missing") from exc
    if not resolved.is_file() or root not in resolved.parents:
        raise StageTwoValidationError(f"{path} must reference a file inside evidence_root")
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual != reference["sha256"]:
        raise StageTwoValidationError(f"{path} sha256 does not match the evidence file")
    expected_artifacts[relative_text] = actual


def _indexed(rows: list[Any], path: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        item = _mapping(row, f"{path}[{index}]")
        item_id = _string(_get(item, "id", f"{path}[{index}]"), f"{path}[{index}].id")
        if item_id in result:
            raise StageTwoValidationError(f"duplicate {path[:-1]} id {item_id!r}")
        result[item_id] = item
    return result


def _mapping_slot(value: Any) -> dict[str, Any]:
    return _mapping(value, "list item")


def _get(value: dict[str, Any], key: str, path: str) -> Any:
    if key not in value:
        raise StageTwoValidationError(f"{path} is missing required key {key!r}")
    return value[key]


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise StageTwoValidationError(f"{path} must be a mapping")
    return value


def _sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise StageTwoValidationError(f"{path} must be a list")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StageTwoValidationError(f"{path} must be a non-empty string")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise StageTwoValidationError(f"{path} must be a boolean")
    return value


def _integer(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StageTwoValidationError(f"{path} must be an integer")
    return value


def _hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_HASH.fullmatch(value))

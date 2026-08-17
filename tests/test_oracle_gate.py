from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from agentcongress.oracle_gate import (
    BackendEvidence,
    ControlTrialEvidence,
    OciImageLock,
    OracleGateError,
    OracleGateEvidence,
    OracleGateRunner,
    PreparedTaskLock,
    REQUIRED_CONTROL_ARTIFACT_KINDS,
)


def _task() -> PreparedTaskLock:
    return PreparedTaskLock(
        suite_id="stage-two-v1",
        suite_sha256="9" * 64,
        task_id="task-1",
        source_revision="a" * 40,
        source_locator="tasks/task-1",
        image=OciImageLock("registry.example/task-1:fixed", "sha256:" + "b" * 64, "linux/amd64"),
        verifier_image=OciImageLock(
            "registry.example/task-1-verifier:fixed", "sha256:" + "f" * 64, "linux/amd64"
        ),
        task_metadata_sha256="c" * 64,
        verifier_sha256="d" * 64,
        solution_sha256="e" * 64,
    )


def _artifact(root: Path, task: PreparedTaskLock, kind: str, payload: bytes | None = None) -> BackendEvidence:
    content = payload if payload is not None else kind.encode()
    path = root / f"{kind}.json"
    path.write_bytes(content)
    return BackendEvidence(kind, "ok", path, hashlib.sha256(content).hexdigest(), task.sha256)


class FakeBackend:
    def __init__(self, root: Path, task: PreparedTaskLock) -> None:
        self.calls: list[str] = []
        self.attestation = _artifact(root, task, "backend_attestation")
        self.oracle = self._trial(root, task, "oracle", True, "oracle")
        self.nop = self._trial(root, task, "isolation_nop", False, "nop")

    @staticmethod
    def _trial(
        root: Path,
        task: PreparedTaskLock,
        control: str,
        success: bool,
        prefix: str,
    ) -> ControlTrialEvidence:
        artifacts = tuple(
            _artifact(root, task, kind, f"{prefix}-{kind}".encode())
            if prefix == "oracle"
            else _named_artifact(root, task, kind, prefix)
            for kind in sorted(REQUIRED_CONTROL_ARTIFACT_KINDS)
        )
        return ControlTrialEvidence(
            control=control,
            job_id=f"{prefix}-job",
            trial_id=f"{prefix}-trial",
            environment_id=f"{prefix}-environment",
            fresh_environment=True,
            objective_success=success,
            status="ok",
            prepared_task_sha256=task.sha256,
            required_artifacts=artifacts,
        )

    def attest(self, task: PreparedTaskLock) -> BackendEvidence:
        self.calls.append("attest")
        return self.attestation

    def run_oracle(self, task: PreparedTaskLock) -> ControlTrialEvidence:
        self.calls.append("oracle")
        return self.oracle

    def run_isolation_nop(self, task: PreparedTaskLock) -> ControlTrialEvidence:
        self.calls.append("isolation-nop")
        return self.nop


def _named_artifact(root: Path, task: PreparedTaskLock, kind: str, prefix: str) -> BackendEvidence:
    payload = f"{prefix}-{kind}".encode()
    path = root / f"{prefix}-{kind}.json"
    path.write_bytes(payload)
    return BackendEvidence(kind, "ok", path, hashlib.sha256(payload).hexdigest(), task.sha256)


def test_gate_runs_fixed_order_and_requires_fresh_distinct_controls(tmp_path: Path) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)

    evidence = OracleGateRunner(backend).run(task)

    assert backend.calls == ["attest", "oracle", "isolation-nop"]
    assert evidence.status == "ok"
    assert evidence.oracle.fresh_environment and evidence.isolation_nop.fresh_environment
    assert evidence.oracle.job_id != evidence.isolation_nop.job_id
    assert evidence.oracle.trial_id != evidence.isolation_nop.trial_id
    assert evidence.oracle.environment_id != evidence.isolation_nop.environment_id


def test_prepared_task_identity_binds_suite_and_separate_images() -> None:
    task = _task()
    baseline = task.sha256

    assert replace(task, suite_id="another-suite").sha256 != baseline
    assert replace(task, suite_sha256="8" * 64).sha256 != baseline
    assert replace(task, image=replace(task.image, digest="sha256:" + "7" * 64)).sha256 != baseline
    assert replace(
        task,
        verifier_image=replace(task.verifier_image, digest="sha256:" + "6" * 64),
    ).sha256 != baseline


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("fresh_environment", False, "fresh environment"),
        ("objective_success", False, "objective_success must be true"),
        ("status", "failed", "status is not ok"),
    ],
)
def test_oracle_failure_stops_before_nop(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    backend.oracle = replace(backend.oracle, **{field: value})

    with pytest.raises(OracleGateError, match=message):
        OracleGateRunner(backend).run(task)

    assert backend.calls == ["attest", "oracle"]


def test_nop_success_fails_closed(tmp_path: Path) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    backend.nop = replace(backend.nop, objective_success=True)

    with pytest.raises(OracleGateError, match="objective_success must be false"):
        OracleGateRunner(backend).run(task)

    assert backend.calls == ["attest", "oracle", "isolation-nop"]


@pytest.mark.parametrize("field", ["job_id", "trial_id", "environment_id"])
def test_controls_must_have_distinct_ids(tmp_path: Path, field: str) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    backend.nop = replace(backend.nop, **{field: getattr(backend.oracle, field)})

    with pytest.raises(OracleGateError, match=f"different {field}"):
        OracleGateRunner(backend).run(task)


def test_missing_attestation_stops_before_controls(tmp_path: Path) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    backend.attestation.path.unlink()

    with pytest.raises(OracleGateError, match="missing"):
        OracleGateRunner(backend).run(task)

    assert backend.calls == ["attest"]


def test_tampered_oracle_artifact_stops_before_nop(tmp_path: Path) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    backend.oracle.required_artifacts[0].path.write_bytes(b"tampered")

    with pytest.raises(OracleGateError, match="sha256 does not match"):
        OracleGateRunner(backend).run(task)

    assert backend.calls == ["attest", "oracle"]


def test_required_artifact_status_must_be_ok(tmp_path: Path) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    failed = replace(backend.oracle.required_artifacts[0], status="failed")
    backend.oracle = replace(backend.oracle, required_artifacts=(failed,))

    with pytest.raises(OracleGateError, match="status is not ok"):
        OracleGateRunner(backend).run(task)

    assert backend.calls == ["attest", "oracle"]


def test_trial_requires_the_fixed_minimum_artifact_kinds(tmp_path: Path) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    backend.oracle = replace(backend.oracle, required_artifacts=backend.oracle.required_artifacts[1:])

    with pytest.raises(OracleGateError, match="missing required artifact kinds"):
        OracleGateRunner(backend).run(task)

    assert backend.calls == ["attest", "oracle"]


def test_agent_and_verifier_images_must_be_separate(tmp_path: Path) -> None:
    task = _task()
    task = replace(task, verifier_image=replace(task.verifier_image, digest=task.image.digest))
    backend = FakeBackend(tmp_path, task)

    with pytest.raises(OracleGateError, match="agent and verifier images must be separate"):
        OracleGateRunner(backend).run(task)

    assert backend.calls == []


def test_simulated_evidence_is_constructible_but_formal_verify_rejects_it(tmp_path: Path) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    runner = OracleGateRunner(backend)
    measured = runner.run(task)
    simulated = replace(measured, evidence_level="simulated")

    with pytest.raises(OracleGateError, match="must be measured"):
        runner.verify(simulated)


def test_simulated_backend_step_stops_immediately(tmp_path: Path) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    backend.attestation = replace(backend.attestation, evidence_level="simulated")

    with pytest.raises(OracleGateError, match="evidence_level must be measured"):
        OracleGateRunner(backend).run(task)

    assert backend.calls == ["attest"]


def test_formal_verify_recomputes_artifacts(tmp_path: Path) -> None:
    task = _task()
    backend = FakeBackend(tmp_path, task)
    runner = OracleGateRunner(backend)
    evidence: OracleGateEvidence = runner.run(task)
    evidence.isolation_nop.required_artifacts[0].path.write_bytes(b"changed later")

    with pytest.raises(OracleGateError, match="sha256 does not match"):
        runner.verify(evidence)

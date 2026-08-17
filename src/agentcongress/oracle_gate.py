from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_REVISION = re.compile(r"[0-9a-fA-F]{40}\Z")
REQUIRED_CONTROL_ARTIFACT_KINDS = frozenset(
    {"config", "lock", "result", "reward", "verifier", "artifacts_manifest"}
)


class OracleGateError(RuntimeError):
    """The sealed zero-model control gate could not be proven."""


@dataclass(frozen=True, slots=True)
class OciImageLock:
    reference: str
    digest: str
    platform: str


@dataclass(frozen=True, slots=True)
class PreparedTaskLock:
    suite_id: str
    suite_sha256: str
    task_id: str
    source_revision: str
    source_locator: str
    image: OciImageLock
    verifier_image: OciImageLock
    task_metadata_sha256: str
    verifier_sha256: str
    solution_sha256: str

    @property
    def sha256(self) -> str:
        """Content identity used to bind every piece of backend evidence."""

        payload = {
            "image": {
                "digest": self.image.digest,
                "platform": self.image.platform,
                "reference": self.image.reference,
            },
            "suite_id": self.suite_id,
            "suite_sha256": self.suite_sha256,
            "solution_sha256": self.solution_sha256,
            "source_locator": self.source_locator,
            "source_revision": self.source_revision,
            "task_id": self.task_id,
            "task_metadata_sha256": self.task_metadata_sha256,
            "verifier_sha256": self.verifier_sha256,
            "verifier_image": {
                "digest": self.verifier_image.digest,
                "platform": self.verifier_image.platform,
                "reference": self.verifier_image.reference,
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BackendEvidence:
    """One required, content-addressed artifact emitted by the sealed backend."""

    kind: str
    status: str
    path: Path
    sha256: str
    prepared_task_sha256: str
    evidence_level: Literal["measured", "simulated"] = "measured"


@dataclass(frozen=True, slots=True)
class ControlTrialEvidence:
    control: Literal["oracle", "isolation_nop"]
    job_id: str
    trial_id: str
    environment_id: str
    fresh_environment: bool
    objective_success: bool
    status: str
    prepared_task_sha256: str
    required_artifacts: tuple[BackendEvidence, ...]
    evidence_level: Literal["measured", "simulated"] = "measured"


@dataclass(frozen=True, slots=True)
class OracleGateEvidence:
    prepared_task: PreparedTaskLock
    backend_attestation: BackendEvidence
    oracle: ControlTrialEvidence
    isolation_nop: ControlTrialEvidence
    status: str = "ok"
    evidence_level: Literal["measured", "simulated"] = "measured"


class SealedControlBackend(Protocol):
    """Backend boundary for controls which never invoke a model or model API."""

    def attest(self, task: PreparedTaskLock) -> BackendEvidence: ...

    def run_oracle(self, task: PreparedTaskLock) -> ControlTrialEvidence: ...

    def run_isolation_nop(self, task: PreparedTaskLock) -> ControlTrialEvidence: ...


class OracleGateRunner:
    """Run attestation, positive control, then negative control, fail closed."""

    def __init__(self, backend: SealedControlBackend) -> None:
        self._backend = backend

    def run(self, task: PreparedTaskLock) -> OracleGateEvidence:
        _verify_task_lock(task)

        attestation = self._call("attest", self._backend.attest, task)
        _verify_artifact(attestation, task.sha256, expected_kind="backend_attestation")

        oracle = self._call("oracle", self._backend.run_oracle, task)
        _verify_trial(oracle, task.sha256, expected_control="oracle", expected_success=True)

        isolation_nop = self._call("isolation-nop", self._backend.run_isolation_nop, task)
        _verify_trial(
            isolation_nop,
            task.sha256,
            expected_control="isolation_nop",
            expected_success=False,
        )
        _verify_distinct_controls(oracle, isolation_nop)

        evidence = OracleGateEvidence(task, attestation, oracle, isolation_nop)
        self.verify(evidence)
        return evidence

    def verify(self, evidence: OracleGateEvidence) -> None:
        """Recompute a completed gate's formal, measured evidence from disk."""

        if not isinstance(evidence, OracleGateEvidence):
            raise OracleGateError("oracle gate evidence has the wrong type")
        _verify_task_lock(evidence.prepared_task)
        if evidence.status != "ok":
            raise OracleGateError("oracle gate status is not ok")
        if evidence.evidence_level != "measured":
            raise OracleGateError("formal oracle gate evidence_level must be measured")
        task_sha256 = evidence.prepared_task.sha256
        _verify_artifact(evidence.backend_attestation, task_sha256, expected_kind="backend_attestation")
        _verify_trial(evidence.oracle, task_sha256, expected_control="oracle", expected_success=True)
        _verify_trial(
            evidence.isolation_nop,
            task_sha256,
            expected_control="isolation_nop",
            expected_success=False,
        )
        _verify_distinct_controls(evidence.oracle, evidence.isolation_nop)

    @staticmethod
    def _call(name: str, operation, task: PreparedTaskLock):
        try:
            return operation(task)
        except Exception as exc:
            raise OracleGateError(f"{name} backend step failed: {exc}") from exc


def _verify_task_lock(task: PreparedTaskLock) -> None:
    if not isinstance(task, PreparedTaskLock):
        raise OracleGateError("prepared task lock has the wrong type")
    for value, field in (
        (task.suite_id, "suite_id"),
        (task.task_id, "task_id"),
        (task.source_locator, "source_locator"),
        (task.image.reference, "image.reference"),
        (task.image.platform, "image.platform"),
        (task.verifier_image.reference, "verifier_image.reference"),
        (task.verifier_image.platform, "verifier_image.platform"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise OracleGateError(f"prepared task {field} must be a non-empty string")
    if not isinstance(task.source_revision, str) or not _GIT_REVISION.fullmatch(task.source_revision):
        raise OracleGateError("prepared task source_revision must be a full Git SHA")
    if not isinstance(task.image.digest, str) or not _OCI_DIGEST.fullmatch(task.image.digest):
        raise OracleGateError("prepared task image digest must be an immutable sha256 OCI digest")
    if not isinstance(task.verifier_image.digest, str) or not _OCI_DIGEST.fullmatch(task.verifier_image.digest):
        raise OracleGateError("prepared task verifier image digest must be an immutable sha256 OCI digest")
    if task.verifier_image.digest == task.image.digest:
        raise OracleGateError("prepared task agent and verifier images must be separate immutable images")
    for value, field in (
        (task.suite_sha256, "suite_sha256"),
        (task.task_metadata_sha256, "task_metadata_sha256"),
        (task.verifier_sha256, "verifier_sha256"),
        (task.solution_sha256, "solution_sha256"),
    ):
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise OracleGateError(f"prepared task {field} must be a lowercase sha256")


def _verify_artifact(
    artifact: BackendEvidence,
    task_sha256: str,
    *,
    expected_kind: str | None = None,
) -> Path:
    if not isinstance(artifact, BackendEvidence):
        raise OracleGateError("required backend artifact has the wrong type")
    if expected_kind is not None and artifact.kind != expected_kind:
        raise OracleGateError(f"required backend artifact kind must be {expected_kind!r}")
    if not isinstance(artifact.kind, str) or not artifact.kind.strip():
        raise OracleGateError("required backend artifact kind must be non-empty")
    if artifact.status != "ok":
        raise OracleGateError(f"required artifact {artifact.kind!r} status is not ok")
    if artifact.evidence_level != "measured":
        raise OracleGateError(f"required artifact {artifact.kind!r} evidence_level must be measured")
    if artifact.prepared_task_sha256 != task_sha256:
        raise OracleGateError(f"required artifact {artifact.kind!r} is bound to the wrong prepared task")
    if not isinstance(artifact.sha256, str) or not _SHA256.fullmatch(artifact.sha256):
        raise OracleGateError(f"required artifact {artifact.kind!r} has an invalid sha256")
    if not isinstance(artifact.path, Path) or not artifact.path.is_absolute():
        raise OracleGateError(f"required artifact {artifact.kind!r} path must be absolute")
    if artifact.path.is_symlink():
        raise OracleGateError(f"required artifact {artifact.kind!r} must not be a symlink")
    try:
        resolved = artifact.path.resolve(strict=True)
    except OSError as exc:
        raise OracleGateError(f"required artifact {artifact.kind!r} is missing") from exc
    if not resolved.is_file():
        raise OracleGateError(f"required artifact {artifact.kind!r} is not a file")
    actual = _sha256_file(resolved)
    if actual != artifact.sha256:
        raise OracleGateError(f"required artifact {artifact.kind!r} sha256 does not match its file")
    return resolved


def _verify_trial(
    trial: ControlTrialEvidence,
    task_sha256: str,
    *,
    expected_control: Literal["oracle", "isolation_nop"],
    expected_success: bool,
) -> None:
    if not isinstance(trial, ControlTrialEvidence):
        raise OracleGateError(f"{expected_control} trial evidence has the wrong type")
    if trial.control != expected_control:
        raise OracleGateError(f"control trial must be {expected_control!r}")
    if trial.status != "ok":
        raise OracleGateError(f"{expected_control} trial status is not ok")
    if trial.evidence_level != "measured":
        raise OracleGateError(f"{expected_control} trial evidence_level must be measured")
    if trial.prepared_task_sha256 != task_sha256:
        raise OracleGateError(f"{expected_control} trial is bound to the wrong prepared task")
    if trial.fresh_environment is not True:
        raise OracleGateError(f"{expected_control} trial did not use a fresh environment")
    if type(trial.objective_success) is not bool or trial.objective_success is not expected_success:
        expectation = "true" if expected_success else "false"
        raise OracleGateError(f"{expected_control} objective_success must be {expectation}")
    for value, field in (
        (trial.job_id, "job_id"),
        (trial.trial_id, "trial_id"),
        (trial.environment_id, "environment_id"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise OracleGateError(f"{expected_control} {field} must be non-empty")
    if not isinstance(trial.required_artifacts, tuple) or not trial.required_artifacts:
        raise OracleGateError(f"{expected_control} trial has no required artifacts")
    kinds: set[str] = set()
    paths: set[Path] = set()
    for artifact in trial.required_artifacts:
        resolved = _verify_artifact(artifact, task_sha256)
        if artifact.kind in kinds:
            raise OracleGateError(f"{expected_control} trial has duplicate required artifact kind {artifact.kind!r}")
        if resolved in paths:
            raise OracleGateError(f"{expected_control} trial reuses a required artifact file")
        kinds.add(artifact.kind)
        paths.add(resolved)
    missing_kinds = sorted(REQUIRED_CONTROL_ARTIFACT_KINDS - kinds)
    if missing_kinds:
        raise OracleGateError(
            f"{expected_control} trial is missing required artifact kinds {missing_kinds}"
        )


def _verify_distinct_controls(oracle: ControlTrialEvidence, isolation_nop: ControlTrialEvidence) -> None:
    for field in ("job_id", "trial_id", "environment_id"):
        if getattr(oracle, field) == getattr(isolation_nop, field):
            raise OracleGateError(f"oracle and isolation-nop controls must have different {field}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

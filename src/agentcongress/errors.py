from __future__ import annotations


class WorkerError(RuntimeError):
    """Base class for failures produced by a model worker session."""


class WorkerInfrastructureError(WorkerError):
    """The worker process, authentication, or sandbox could not run."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class WorkerTimeoutError(WorkerError):
    """A worker exhausted its fixed wall-clock slot."""


class WorkerProtocolError(WorkerError):
    """A worker finished without the required structured handoff."""


class WorkerHumanInputRequired(WorkerError):
    """A valid worker report explicitly requires an operator decision."""


class WorkerValidationError(WorkerError):
    """A worker submission failed harness-owned validation."""

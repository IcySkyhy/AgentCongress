from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelRates:
    """API-equivalent USD rates per million tokens, frozen into each run manifest."""

    input_per_million: float
    cached_input_per_million: float
    output_per_million: float


# This is deliberately a versioned default, not a claim about a user's Codex
# subscription charge. Callers persist the chosen rates in the run manifest.
DEFAULT_MODEL_RATES: dict[str, ModelRates] = {
    "gpt-5.6-luna": ModelRates(1.0, 0.1, 6.0),
    "gpt-5.6-sol": ModelRates(5.0, 0.5, 30.0),
}


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Usage | None":
        """Read common Codex/OpenAI JSONL usage shapes without guessing missing data."""
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None

        def integer(*names: str) -> int:
            for name in names:
                value = usage.get(name)
                if isinstance(value, int) and value >= 0:
                    return value
            return 0

        details = usage.get("input_tokens_details")
        cached_from_details = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        cached = integer("cached_input_tokens", "cached_tokens") or (cached_from_details if isinstance(cached_from_details, int) and cached_from_details >= 0 else 0)
        value = cls(
            input_tokens=integer("input_tokens", "prompt_tokens"),
            cached_input_tokens=cached,
            output_tokens=integer("output_tokens", "completion_tokens"),
            reasoning_tokens=integer("reasoning_tokens"),
        )
        return value if any(asdict(value).values()) else None

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Budget:
    max_worker_sessions: int
    max_wall_seconds: float
    max_estimated_cost_usd: float | None = None

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


class BudgetGovernor:
    """A run-level governor with hard session/time limits and usage accounting."""

    def __init__(self, budget: Budget, model: str, rates: ModelRates | None = None) -> None:
        if budget.max_worker_sessions < 1:
            raise ValueError("max_worker_sessions must be at least one")
        if budget.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be positive")
        self.budget = budget
        self.model = model
        self.rates = rates or DEFAULT_MODEL_RATES.get(model)
        self.started_at = time.monotonic()
        self.worker_sessions = 0
        self.worker_elapsed_seconds = 0.0
        self.session_elapsed_seconds: list[float] = []
        self._active_session_started_at: float | None = None
        self.usage = Usage()
        self.usage_by_model: dict[str, Usage] = {}
        self.usage_events = 0

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def estimated_cost_usd(self) -> float | None:
        total = 0.0
        observed_models = self.usage_by_model or {self.model: self.usage}
        for model, usage in observed_models.items():
            rates = self.rates if model == self.model else DEFAULT_MODEL_RATES.get(model)
            if rates is None:
                return None
            uncached_input = max(0, usage.input_tokens - usage.cached_input_tokens)
            total += (
                uncached_input * rates.input_per_million
                + usage.cached_input_tokens * rates.cached_input_per_million
                # Reasoning tokens are reported as a detail of output usage by
                # OpenAI-compatible payloads, so they must not be billed twice.
                + usage.output_tokens * rates.output_per_million
            ) / 1_000_000
        return total

    @property
    def remaining_seconds(self) -> float:
        active = 0.0 if self._active_session_started_at is None else time.monotonic() - self._active_session_started_at
        return max(0.0, self.budget.max_wall_seconds - self.worker_elapsed_seconds - active)

    def assert_can_start_session(self) -> None:
        if self.worker_sessions >= self.budget.max_worker_sessions:
            raise BudgetExceeded("worker session budget exhausted")
        if self.remaining_seconds <= 0:
            raise BudgetExceeded("wall-clock budget exhausted")
        cost = self.estimated_cost_usd
        if self.budget.max_estimated_cost_usd is not None and cost is not None and cost >= self.budget.max_estimated_cost_usd:
            raise BudgetExceeded("estimated cost budget exhausted")

    def start_session(self, max_seconds: float | None = None) -> float:
        if self._active_session_started_at is not None:
            raise RuntimeError("a worker session is already active")
        self.assert_can_start_session()
        if max_seconds is not None and max_seconds <= 0:
            raise ValueError("max_seconds must be positive when provided")
        self.worker_sessions += 1
        # Reserve an equal share of remaining time for the sessions that remain.
        sessions_left = self.budget.max_worker_sessions - self.worker_sessions + 1
        reserved = max(1.0, self.remaining_seconds / sessions_left)
        self._active_session_started_at = time.monotonic()
        return min(reserved, max_seconds) if max_seconds is not None else reserved

    def finish_session(self) -> float:
        if self._active_session_started_at is None:
            raise RuntimeError("there is no active worker session")
        elapsed = time.monotonic() - self._active_session_started_at
        self.worker_elapsed_seconds += elapsed
        self.session_elapsed_seconds.append(elapsed)
        self._active_session_started_at = None
        return elapsed

    def observe_event(self, payload: dict[str, Any], *, model: str | None = None) -> Usage | None:
        # `turn.completed` is the only event we count: intermediate events often
        # repeat the same cumulative usage payload.
        if payload.get("type") not in {"turn.completed", "response.completed"}:
            return None
        usage = Usage.from_payload(payload)
        if usage is not None:
            self.usage.add(usage)
            usage_model = model or self.model
            self.usage_by_model.setdefault(usage_model, Usage()).add(usage)
            self.usage_events += 1
        return usage

    def assert_cost_within_limit(self) -> None:
        cost = self.estimated_cost_usd
        maximum = self.budget.max_estimated_cost_usd
        if maximum is not None and cost is not None and cost > maximum:
            raise BudgetExceeded("estimated cost budget exceeded")

    def snapshot(self) -> dict[str, Any]:
        return {
            "budget": self.budget.as_dict(),
            "model": self.model,
            "rates_per_million": asdict(self.rates) if self.rates else None,
            "worker_sessions": self.worker_sessions,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "worker_elapsed_seconds": round(self.worker_elapsed_seconds, 3),
            "session_elapsed_seconds": [round(value, 3) for value in self.session_elapsed_seconds],
            "usage": self.usage.as_dict(),
            "usage_by_model": {model: usage.as_dict() for model, usage in sorted(self.usage_by_model.items())},
            "usage_events": self.usage_events,
            "rates_per_million_by_model": {
                model: asdict(self.rates if model == self.model else DEFAULT_MODEL_RATES.get(model))
                if (self.rates if model == self.model else DEFAULT_MODEL_RATES.get(model))
                else None
                for model in sorted(self.usage_by_model)
            },
            "estimated_api_equivalent_cost_usd": self.estimated_cost_usd,
        }

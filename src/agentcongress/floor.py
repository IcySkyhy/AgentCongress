from __future__ import annotations

from dataclasses import dataclass, field

from .models import FloorRequest


@dataclass(slots=True)
class FloorPolicy:
    grant_threshold: float = 0.65
    tie_delta: float = 0.05
    max_consecutive_grants: int = 2
    speaking_turns: dict[str, int] = field(default_factory=dict)
    consecutive_grants: dict[str, int] = field(default_factory=dict)

    def score(self, request: FloorRequest) -> float:
        fairness = 1 / (1 + self.speaking_turns.get(request.agent_id, 0))
        cooldown = self.consecutive_grants.get(request.agent_id, 0) / self.max_consecutive_grants
        disruption = max(0, request.estimated_segments - 1) / 2
        score = (.30 * request.urgency + .25 * request.relevance + .20 * request.novelty + .10 * request.confidence + .10 * fairness + .05 * float(request.explicitly_addressed) - .15 * disruption - .10 * cooldown)
        return max(0.0, min(1.0, score))

    def select(self, requests: list[FloorRequest]) -> FloorRequest | None:
        ranked = sorted(((self.score(item), item) for item in requests), key=lambda pair: (-pair[0], pair[1].agent_id))
        if not ranked or ranked[0][0] < self.grant_threshold:
            return None
        top_score = ranked[0][0]
        tied = [item for score, item in ranked if top_score - score <= self.tie_delta]
        # Near-ties deliberately favor the participant who has spoken less.
        return min(tied, key=lambda item: (self.speaking_turns.get(item.agent_id, 0), item.agent_id))

    def grant(self, agent_id: str) -> None:
        previous = self.consecutive_grants.get(agent_id, 0)
        for other in list(self.consecutive_grants):
            if other != agent_id:
                self.consecutive_grants[other] = 0
        self.consecutive_grants[agent_id] = previous + 1
        self.speaking_turns[agent_id] = self.speaking_turns.get(agent_id, 0) + 1

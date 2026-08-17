from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SentenceSegmenter:
    """Stateful stream splitter that never cuts inside fenced code blocks."""

    max_chars: int = 600
    _buffer: str = ""
    _in_fence: bool = False

    def push(self, chunk: str) -> list[str]:
        self._buffer += chunk
        segments: list[str] = []
        start = 0
        index = 0
        while index < len(self._buffer):
            char = self._buffer[index]
            if self._buffer[index:index + 3] == "```":
                self._in_fence = not self._in_fence
                index += 3
                continue
            is_boundary = char in ".?!\n" or char in {"。", "！", "？"}
            if not self._in_fence and is_boundary:
                candidate = self._buffer[start:index + 1].strip()
                if candidate:
                    segments.append(candidate)
                start = index + 1
            elif not self._in_fence and index + 1 - start >= self.max_chars:
                candidate = self._buffer[start:index + 1].strip()
                if candidate:
                    segments.append(candidate)
                start = index + 1
            index += 1
        self._buffer = self._buffer[start:]
        return segments

    def flush(self) -> str | None:
        remaining = self._buffer.strip()
        self._buffer = ""
        return remaining or None


@dataclass(frozen=True, slots=True)
class ListenerProfile:
    agent_id: str
    capability_tags: frozenset[str] = field(default_factory=frozenset)
    role: str = "meeting participant"


class ListenerGate:
    """Cheap per-listener filter; it never grants a floor by itself."""

    def should_evaluate(self, profile: ListenerProfile, segment: str, segment_number: int) -> bool:
        normalized = segment.casefold()
        if f"@{profile.agent_id.casefold()}" in normalized:
            return True
        if any(tag.casefold() in normalized for tag in profile.capability_tags):
            return True
        return segment_number % 3 == 0

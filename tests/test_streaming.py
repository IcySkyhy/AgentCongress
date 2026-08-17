from agentcongress.streaming import ListenerGate, ListenerProfile, SentenceSegmenter


def test_segments_english_and_chinese_text() -> None:
    segmenter = SentenceSegmenter()
    assert segmenter.push("First. 第二句！") == ["First.", "第二句！"]


def test_does_not_cut_inside_fenced_code() -> None:
    segmenter = SentenceSegmenter()
    assert segmenter.push("```python\nprint('x.')\n```\nDone.") == ["```python\nprint('x.')\n```", "Done."]


def test_listener_gate_preserves_agent_autonomy() -> None:
    gate = ListenerGate()
    profile = ListenerProfile("reviewer", frozenset({"security"}))
    assert gate.should_evaluate(profile, "@reviewer please inspect", 1)
    assert gate.should_evaluate(profile, "There is a security issue.", 1)
    assert gate.should_evaluate(profile, "Unrelated", 3)

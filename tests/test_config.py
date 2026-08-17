from pathlib import Path

import pytest

from agentcongress.config import load_config


def test_loads_yaml_meeting_config(tmp_path: Path) -> None:
    config = tmp_path / "meeting.yaml"
    config.write_text("""meeting:\n  id: demo\n  initial_speaker: architect\n  initial_addressee: reviewer\n  execution_mode: continuous\n  agents:\n    - id: architect\n      role: designs the system\n      capability_tags: [architecture]\n    - id: reviewer\n      role: checks changes\n""", encoding="utf-8")
    loaded = load_config(config)
    assert loaded.roster == ["architect", "reviewer"]
    assert loaded.execution_mode == "continuous"


def test_rejects_duplicate_agent_ids(tmp_path: Path) -> None:
    config = tmp_path / "meeting.yaml"
    config.write_text("""id: demo\ninitial_speaker: a\ninitial_addressee: b\nagents:\n  - {id: a, role: one}\n  - {id: a, role: two}\n""", encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_config(config)

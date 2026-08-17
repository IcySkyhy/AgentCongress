import subprocess
from pathlib import Path

from agentcongress.models import Task, TaskReport
from agentcongress.verification import _verification_environment, verify_task


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_verifier_rejects_changes_outside_allowed_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    base = _git_output(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text("assert False\n", encoding="utf-8")
    task = Task("change", "Change app", "worker", ["keep tests"], ["src/"], [])
    report = TaskReport("done", ("src/app.py", "tests/test_app.py"), (), (), None, False)
    result = verify_task(repo, task, report, base)
    assert not result.passed
    assert "allowed_paths" in result.errors[0]


def test_verifier_includes_untracked_files_in_the_deliverable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    base = _git_output(repo, "rev-parse", "HEAD")
    (repo / "generated.py").write_text("value = 1\n", encoding="utf-8")
    task = Task("change", "Change app", "worker", ["add module"], ["src/"], [])
    report = TaskReport("done", (), (), (), None, False)

    result = verify_task(repo, task, report, base)

    assert not result.passed
    assert result.changed_files == ("generated.py",)
    assert any("outside allowed_paths" in error for error in result.errors)
    assert any("does not match" in error for error in result.errors)


def test_verifier_environment_does_not_forward_credentials(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    environment = _verification_environment()
    assert "DEEPSEEK_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "PATH" in environment


def _git_output(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()

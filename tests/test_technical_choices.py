import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

import pytest

from agent_office import away_expert
from agent_office.adapters import AgentResult
from agent_office.away_evidence import DelegationDeferred
from agent_office.technical_choices import CHOICE_MARKER, assess_choice, parse_choice, render_choice


def envelope(purpose="implementation", options=None):
    return CHOICE_MARKER + "\n" + json.dumps({
        "purpose": purpose, "options": options or [{"path": "src/current.py", "line": 1},
                                                   {"path": "src/other.py", "line": 1}],
    })


@pytest.mark.parametrize("purpose", ["product", "permission", "deployment", "scope"])
def test_only_bounded_technical_purposes_are_eligible(purpose):
    with pytest.raises(ValueError):
        parse_choice(envelope(purpose))


@pytest.mark.parametrize("invalid", [
    {"path": "../src/current.py", "line": 1},
    {"path": "src/current.py; command", "line": 1},
    {"path": ".env", "line": 1},
    {"path": "src/secrets.py", "line": 1},
    {"path": "src/current.py", "line": True},
    {"path": "src/current.py", "line": 1, "action": "approve"},
])
def test_technical_choice_rejects_scope_paths_and_arbitrary_actions(invalid):
    with pytest.raises(ValueError):
        parse_choice(envelope(options=[invalid, {"path": "src/other.py", "line": 1}]))


def test_validation_choices_require_existing_test_paths():
    test_paths = [{"path": "src/__tests__/present.js", "line": 1},
                  {"path": "src/other.spec.ts", "line": 1}]
    assert parse_choice(envelope("validation", test_paths))["options"] == test_paths
    with pytest.raises(ValueError):
        parse_choice(envelope("validation"))


@pytest.fixture
def source_repo(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True).stdout.decode().strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.org")
    source = tmp_path / "src"
    source.mkdir()
    (source / "current.py").write_text("def supports_goal():\n    return True\n", encoding="utf-8")
    (source / "other.py").write_text("def different_behavior():\n    return False\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "fixture")
    return tmp_path, git("rev-parse", "HEAD")


@pytest.mark.parametrize("case", ["supported", "different_choice", "operator_required", "outside_scope"])
def test_choices_need_grounded_independent_agreement(source_repo, monkeypatch, case):
    repo, revision = source_repo
    choice = parse_choice(envelope())
    (repo / "src" / "current.py").write_text("private dirty data\n", encoding="utf-8")
    calls = []

    def respond(engine, prompt, **kwargs):
        calls.append(engine)
        assert kwargs["allow_edits"] is False
        assert "private dirty data" not in prompt
        choices = json.loads(prompt.splitlines()[-1])["options"]
        selection = choices[1] if case == "different_choice" and engine == "antigravity" else choices[0]
        decision = {"option": selection["option"], "line": selection["line"], "quote": selection["quote"]}
        if engine == "antigravity":
            decision["verdict"] = "operator_required" if case == "operator_required" else "verified"
        return AgentResult(0, json.dumps(decision))

    monkeypatch.setattr(away_expert, "execute_fixture", respond)
    folders = ["unselected"] if case == "outside_scope" else ["src"]
    kwargs = dict(folders=folders, goal="Reuse the implementation for the documented behavior.")
    if case == "supported":
        out = assess_choice(repo, revision, choice, "codex", "antigravity", **kwargs)
        assert out["evidence"] == {"purpose": "implementation", "option": 1,
                                   "path": "src/current.py", "line": 1, "quote": "def supports_goal():",
                                   "revision": revision}
        assert "src/current.py:1" in out["answer"] and "src/other.py" not in out["answer"]
        assert "src/current.py:1" in render_choice(choice)
    else:
        with pytest.raises(DelegationDeferred):
            assess_choice(repo, revision, choice, "codex", "antigravity", **kwargs)
    assert calls == ([] if case == "outside_scope" else ["codex", "antigravity"])


def test_choice_runs_two_read_only_subprocesses_in_separate_evidence_directories(source_repo, monkeypatch):
    from agent_office.away_consult import run_consultation
    from agent_office.technical_choices import CHOICE_TOPIC

    repo, revision = source_repo
    helper = Path(__file__).parent / "helpers" / "away_roles.py"
    vendor = repo / "mock cli"
    vendor.mkdir()
    launcher = vendor / "claude"
    if os.name == "nt":
        launcher.with_suffix(".cmd").write_text(f'@"{sys.executable}" "{helper}" claude %*\n')
    else:
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} claude "$@"\n')
        launcher.chmod(0o700)
    git_dir = Path(shutil.which("git")).parent
    monkeypatch.setenv("PATH", os.pathsep.join((str(vendor), str(git_dir))))
    calls = repo / "calls"
    monkeypatch.setenv("OFFICE_AWAY_CALLS", str(calls))

    rule = {"topic": CHOICE_TOPIC, "choice": parse_choice(envelope()), "goal": "Continue the existing implementation."}
    result = asyncio.run(run_consultation(repo, revision, rule, "claude", "claude", ["src"]))
    assert result["evidence"]["path"] == "src/current.py"
    specialist, verifier = map(json.loads, calls.read_text().splitlines())
    assert specialist[0] == "AWAY_SPECIALIST" and verifier[0] == "AWAY_VERIFIER"
    assert specialist[1] != verifier[1]
    assert not Path(specialist[1]).exists() and not Path(verifier[1]).exists()

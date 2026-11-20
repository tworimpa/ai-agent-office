import sys
import pytest
from pathlib import Path

from agent_office.adapters import AgentResult
from agent_office.workflow import build_graph


class MockAdapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, prompt, *, workspace, allow_edits, inbox=None):
        self.calls.append((prompt, allow_edits))
        return self.responses.pop(0)


def base_state(tmp_path):
    return {
        "task": "fix bug", "workspace": str(tmp_path),
        "developer": "claude", "reviewer": "codex",
        "test_argv": [sys.executable, "-c", "print('ok')"],
        "allow_edits": True, "attempts": 0, "max_attempts": 2,
    }


def test_develop_test_review(tmp_path):
    developer = MockAdapter([AgentResult(0, "implemented")])
    reviewer = MockAdapter([AgentResult(0, "VERDICT: APPROVED")])
    graph = build_graph({"claude": developer, "codex": reviewer})
    result = graph.invoke(base_state(tmp_path))
    assert result["status"] == "READY_FOR_HUMAN_REVIEW"
    assert result["attempts"] == 1
    assert len(developer.calls) == 1
    assert reviewer.calls[0][1] is False


@pytest.mark.parametrize(("evidence_available", "interactive"), [(True, True), (True, False), (False, True)])
def test_windows_service_review_receives_coordinator_git_evidence(tmp_path, monkeypatch, evidence_available, interactive):
    from types import SimpleNamespace

    def collect():
        if not evidence_available:
            return None
        return " M notes.md", "diff --git a/notes.md b/notes.md\n-old\n+new"

    monkeypatch.setattr("agent_office.workflow.windows_service_session", lambda: True)
    developer = MockAdapter([AgentResult(0, "implemented")])
    reviewer = MockAdapter([AgentResult(0, "VERDICT: APPROVED")])
    def channel(*_):
        return SimpleNamespace(expire=lambda: None)

    graph = build_graph({"claude": developer, "codex": reviewer}, studio_preview=collect,
                        inbox_factory=channel if interactive else None)
    graph.invoke(base_state(tmp_path))
    review, allow_edits = reviewer.calls[0]
    assert not allow_edits and "Test results:\nok" in review
    assert "Git evidence collected by the Studio coordinator" in review
    assert ("cmd.exe" in review) is not interactive
    if evidence_available:
        assert " M notes.md" in review and "-old\n+new" in review
    else:
        assert "Git evidence unavailable" in review
    assert "Git evidence" not in developer.calls[0][0]


def test_retry_after_review_changes(tmp_path):
    developer = MockAdapter([AgentResult(0, "first"), AgentResult(0, "fixed")])
    reviewer = MockAdapter([
        AgentResult(0, "VERDICT: CHANGES_REQUESTED\nPlease fix."),
        AgentResult(0, "VERDICT: APPROVED"),
    ])
    graph = build_graph({"claude": developer, "codex": reviewer})
    result = graph.invoke(base_state(tmp_path))
    assert result["status"] == "READY_FOR_HUMAN_REVIEW"
    assert result["attempts"] == 2
    assert "Please fix." in developer.calls[1][0]


def test_failed_cli_stops(tmp_path):
    developer = MockAdapter([AgentResult(127, "missing CLI")])
    reviewer = MockAdapter([])
    graph = build_graph({"claude": developer, "codex": reviewer})
    result = graph.invoke(base_state(tmp_path))
    assert result["status"] == "FAILED"
    assert len(reviewer.calls) == 0


def test_plan_only_does_not_test_or_review(tmp_path):
    developer = MockAdapter([AgentResult(0, "plan only")])
    reviewer = MockAdapter([])
    state = base_state(tmp_path)
    state["allow_edits"] = False
    graph = build_graph({"claude": developer, "codex": reviewer})
    result = graph.invoke(state)
    assert result["status"] == "PLAN_ONLY"
    assert len(reviewer.calls) == 0


def test_unparseable_review_requires_human(tmp_path):
    developer = MockAdapter([AgentResult(0, "implemented")])
    reviewer = MockAdapter([AgentResult(0, "seems fine")])
    graph = build_graph({"claude": developer, "codex": reviewer})
    result = graph.invoke(base_state(tmp_path))
    assert result["status"] == "NEEDS_HUMAN_REVIEW"


def test_test_failure_stops_at_attempt_limit(tmp_path):
    developer = MockAdapter([AgentResult(0, "implemented"), AgentResult(0, "fixed")])
    reviewer = MockAdapter([])
    state = base_state(tmp_path)
    state["test_argv"] = [sys.executable, "-c", "import sys; sys.exit(1)"]
    graph = build_graph({"claude": developer, "codex": reviewer})
    result = graph.invoke(state)
    assert result["status"] == "TEST_FAILED"
    assert result["attempts"] == 2


@pytest.mark.parametrize("verdict", [
    "VERDICT: APPROVED\nVERDICT: CHANGES_REQUESTED",
    "VERDICT: APPROVED\nVERDICT: APPROVED",
    "VERDICT: UNKNOWN\nVERDICT: APPROVED",
])
def test_conflicting_or_multiple_verdicts_require_human(tmp_path, verdict):
    graph = build_graph({"claude": MockAdapter([AgentResult(0, "done")]),
                         "codex": MockAdapter([AgentResult(0, verdict)])})
    assert graph.invoke(base_state(tmp_path))["status"] == "NEEDS_HUMAN_REVIEW"

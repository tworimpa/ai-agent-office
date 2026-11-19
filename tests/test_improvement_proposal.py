import json
import subprocess
from copy import deepcopy

import pytest

from agent_office import improvement_proposal as proposals
from agent_office.adapters import AgentResult
from agent_office.models import OwnerInfo, TaskSpec
from agent_office.store import TaskStore
from agent_office.workspace import Worktree


RUN = "1" * 32
OTHER = "2" * 32
PATCH = "diff --git a/tool.py b/tool.py\n--- a/tool.py\n+++ b/tool.py\n@@ -1 +1 @@\n-print('error')\n+print('test error')\n"


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def fixture(tmp_path):
    repo = tmp_path / "한글 repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Test")
    (repo / "tool.py").write_text("print('error')\n", encoding="utf-8")
    git(repo, "add", "tool.py")
    git(repo, "commit", "-qm", "initial")
    base = git(repo, "rev-parse", "HEAD").decode("ascii")
    # Both a newer commit and uncommitted user work must stay out of the proposal environment.
    (repo / "new").write_text("newer")
    git(repo, "add", "new")
    git(repo, "commit", "-qm", "new revision")
    (repo / "tool.py").write_text("local edits")
    db = tmp_path / "journal"
    with TaskStore(db) as store:
        for digit, run_id, request in (("3", RUN, "TOKEN=hidden 실패 보고 개선 " + "한" * 900),
                                       ("4", OTHER, "Unrelated work")):
            task = TaskSpec(digit * 32, str(repo), request, "codex", "antigravity", 1)
            store.create_run(task, run_id, Worktree(tmp_path / digit, "work"), OwnerInfo("host", "private-owner"))
            for i in range(15):
                store.record_transition(run_id, operation_key=f"phase-{i}", event_type="WORKSPACE_READY",
                                        payload={"phase": "workspace"})
            store.record_transition(run_id, operation_key="done", event_type="RUN_FINISHED",
                                    state="FAILED", outcome="TEST_FAILED", payload={"phase": "run"})
    return db, repo, base


def answer(history):
    return {"title": "Identify the failed test", "rationale": "The selected run ended with a test failure.",
            "expected": "The failure message identifies its stage.", "patch": PATCH,
            "citations": [{"run_id": row["run_id"], "event": row["events"][-1]} for row in history]}


def test_pinned_independent_checkout_and_minimal_readonly_evidence(fixture, monkeypatch):
    db, repo, base = fixture
    paths = []
    def worker(engine, prompt, *, workspace, allow_edits, timeout):
        assert engine == "codex" and allow_edits is False
        paths.append(workspace)
        assert workspace.resolve() != repo.resolve()
        assert (workspace / ".git").is_dir() and git(workspace, "remote") == b""
        assert not (workspace / ".git" / "objects" / "info" / "alternates").exists()
        assert git(workspace, "rev-parse", "HEAD").decode("ascii") == base
        assert (workspace / "tool.py").read_text() == "print('error')\n"
        assert not (workspace / "new").exists()
        assert not any(secret in prompt for secret in ("hidden", "private-owner", "Unrelated work", str(db)))
        history = json.loads(prompt.splitlines()[-1])
        assert [row["run_id"] for row in history] == [RUN]
        assert len(history[0]["events"]) == proposals.MAX_EVENTS
        assert history[0]["request"].endswith("[truncated]") and "[redacted]" in history[0]["request"]
        assert len(history[0]["request"].encode()) <= proposals.REQUEST_BYTES
        return AgentResult(0, json.dumps(answer(history)))
    monkeypatch.setattr(proposals, "execute_fixture", worker)
    result = proposals.create_proposal(db, repo, base, [RUN], "codex")
    assert result["patch"] == PATCH and result["history"][0]["citation"] == 17
    assert proposals.validate_proposal(result) == result
    with TaskStore(db, readonly=True) as source:
        assert source.get_run(RUN).next_seq == 18
        assert source.connection.execute("SELECT count(*) FROM improvement_receipts").fetchone()[0] == 1
    assert (repo / "tool.py").read_text() == "local edits"
    assert not paths[0].exists()


def test_import_rechecks_request_and_event_provenance_without_provider(fixture, monkeypatch):
    db, repo, base = fixture
    monkeypatch.setattr(proposals, "execute_fixture", lambda engine, prompt, **opts:
                        AgentResult(0, json.dumps(answer(json.loads(prompt.splitlines()[-1])))))
    artifact = proposals.create_proposal(db, repo, base, [RUN, OTHER], "codex")
    monkeypatch.setattr(proposals, "execute_fixture", lambda *a, **kw: pytest.fail("unexpected CLI"))
    proposals.verify_history(db, artifact)
    for field, value in (("title", "New title"), ("engine", "gemini"), ("patch", PATCH.replace("+print", "+ print"))):
        with pytest.raises(ValueError, match="generation receipt"):
            proposals.verify_history(db, dict(artifact, **{field: value}))
    for field in ("request", "events", "run_id"):
        imported = deepcopy(artifact)
        row = imported["history"][0]
        if field == "request":
            row[field] = "A different but valid request."
        elif field == "events":
            row[field][0]["payload"]["duration_ms"] = 89
        else:
            row[field] = "e" * 32
        # Individually plausible records still need the trusted source journal.
        proposals.validate_proposal(imported)
        with pytest.raises((ValueError, RuntimeError)):
            proposals.verify_history(db, imported)


@pytest.mark.parametrize("mode", ["unknown-run", "missing-run", "duplicate-run", "invented-event",
                                  "bool-sequence", "unknown-prose-run", "failed", "truncated", "duplicate-key",
                                  "empty-patch", "wrong-base-patch", "empty-expectation", "wrote"])
def test_unsupported_evidence_or_unusable_response_fails(fixture, monkeypatch, mode):
    db, repo, base = fixture
    def worker(engine, prompt, *, workspace, **kwargs):
        response = answer(json.loads(prompt.splitlines()[-1]))
        citations = response["citations"]
        if mode == "unknown-run":
            citations[0]["run_id"] = "f" * 32
        elif mode == "missing-run":
            citations.pop()
        elif mode == "duplicate-run":
            citations[1] = citations[0]
        elif mode == "invented-event":
            citations[0]["event"]["payload"]["returncode"] = 91
        elif mode == "bool-sequence":
            citations[0]["event"]["seq"] = True
        elif mode == "unknown-prose-run":
            response["rationale"] += " See run " + "f" * 32
        elif mode == "empty-patch":
            response["patch"] = " "
        elif mode == "wrong-base-patch":
            response["patch"] = PATCH.replace("-print('error')", "-print('success')")
        elif mode == "empty-expectation":
            response["expected"] = " "
        elif mode == "wrote":
            (workspace / "tool.py").write_text("changed")
        output = json.dumps(response)
        if mode == "duplicate-key":
            output = '{"title":"other",' + output[1:]
        return AgentResult(int(mode == "failed"), output, truncated=mode == "truncated")
    monkeypatch.setattr(proposals, "execute_fixture", worker)
    with pytest.raises((ValueError, RuntimeError, subprocess.CalledProcessError)):
        proposals.create_proposal(db, repo, base, [RUN, OTHER], "codex")
    with TaskStore(db, readonly=True) as source:
        assert not source.connection.execute("SELECT 1 FROM sqlite_master WHERE name='improvement_receipts'").fetchone()


def test_analysis_can_finish_without_proposing_a_change(fixture, monkeypatch):
    db, repo, base = fixture
    monkeypatch.setattr(proposals, "execute_fixture", lambda *a, **kw: AgentResult(0, "{}"))
    before = db.read_bytes()
    with pytest.raises(proposals.NoImprovementSuggested):
        proposals.create_proposal(db, repo, base, [RUN], "codex")
    assert db.read_bytes() == before


def test_active_run_missing_db_and_duplicates_do_not_start_a_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(proposals, "execute_fixture", lambda *args, **kwargs: pytest.fail("unexpected CLI"))
    db = tmp_path / "absent"
    args = (db, tmp_path, "a" * 40)
    with pytest.raises(RuntimeError, match="DB_NOT_FOUND"):
        proposals.create_proposal(*args, [RUN], "codex")
    assert not db.exists()
    with TaskStore(db) as store:
        store.create_run(TaskSpec("f" * 32, "test", "pending", "codex", "claude", 1),
                         RUN, Worktree(tmp_path, "work"), OwnerInfo("host", "owner"))
    for ids in ([RUN], [RUN, RUN], [], ["wrong"]):
        with pytest.raises(ValueError):
            proposals.create_proposal(*args, ids, "codex")


@pytest.mark.parametrize("field,value", [
    ("base", "HEAD"), ("engine", ["codex"]), ("version", True), ("unexpected", 1), ("title", " "),
    ("expected", "안" * 801), ("rationale", "Observation\x1b[2J"), ("title", "\ud800"),
    ("history", []), ("patch", "update this file"), ("patch", PATCH * 512),
])
def test_import_schema_is_bounded(field, value):
    event = {"seq": 2, "type": "RUN_FINISHED", "payload": {"state": "FAILED", "outcome": "TEST_FAILED", "attempt": 1}}
    history = [{"run_id": RUN, "request": "한글 요청", "state": "FAILED", "outcome": "TEST_FAILED",
                "events": [event], "citation": 2}]
    doc = {**answer(history), "history": history, "base": "a" * 40, "version": 1, "engine": "codex"}
    del doc["citations"]
    proposals.validate_proposal(doc)
    doc[field] = value
    with pytest.raises(ValueError):
        proposals.validate_proposal(doc)

import json
import os
import subprocess
import threading

import pytest

from agent_office.adapters import AgentResult
from agent_office.meeting_parallel import execute_parallel


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, encoding="utf-8").strip()


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "test@example.org")
    for name in ("first", "second", "first/child"):
        folder = repo / name
        folder.mkdir()
        (folder / "old.py").write_bytes(b"x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    plan = {"outcome": "작업", "scope": ["두 모듈"], "rationale": ["독립 모듈"], "questions": [],
            "evidence": {"revision": git(repo, "rev-parse", "HEAD"), "paths": []},
            "criteria": [{"criterion": "두 파일 생성", "verification": "pytest 확인"}], "tasks": [
                {"id": key, "owner": "developer", "description": key, "depends_on": depends, "folders": [folder]}
                for key, folder, depends in (("A", "first", []), ("B", "second", []), ("C", "first", ["A", "B"]))
            ]}
    return repo, plan


class Worker:
    def __init__(self, repo, mutation=None):
        self.barrier = threading.Barrier(2)
        self.roots = []
        self.repo = repo
        self.mutation = mutation

    def execute(self, prompt, *, workspace, allow_edits, strict_completion):
        assert allow_edits and strict_completion
        item = json.loads(prompt.split("\n")[-1])["assigned"]
        self.roots.append(workspace)
        assert workspace != self.repo and not workspace.is_relative_to(self.repo)
        assert git(workspace, "rev-parse", "HEAD") == git(self.repo, "rev-parse", "HEAD")
        self.barrier.wait(timeout=10)
        folder = workspace / item["folders"][0]
        (folder / "old.py").unlink()
        (folder / "new.py").write_bytes(b"x = 2\n")
        if self.mutation and item["id"] == "B":
            return self.mutation(workspace)
        return AgentResult(0, "구현했습니다.")


def execute(repo, plan, worker, logs):
    return execute_parallel(plan, repo, {"codex": worker}, "codex", task="요청 및 선택 답변",
                            record=lambda *row: logs.append(row))


def test_imports_both_independent_worktrees_and_hands_off_dependencies(setup):
    repo, plan = setup
    logs = []
    worker = Worker(repo)
    prior = git(repo, "worktree", "list", "--porcelain")
    result = execute(repo, plan, worker, logs)
    receipt = json.loads(result.split("\n", 1)[1])
    assert [item["id"] for item in receipt["applied"]] == ["A", "B"]
    assert [item["id"] for item in receipt["remaining"]] == ["C"]
    assert len(set(worker.roots)) == 2 and not any(root.exists() for root in worker.roots)
    for folder in ("first", "second"):
        assert (repo / folder / "new.py").read_text() == "x = 2\n"
        assert not (repo / folder / "old.py").exists()
    assert "new.py" in logs[-1][1]
    assert not git(repo, "diff", "--cached", "--name-only")
    assert git(repo, "worktree", "list", "--porcelain") == prior


def test_worker_merges_into_linked_parent_without_changing_tracked_executable_mode(setup):
    source, plan = setup
    exe = source / "second/script.py"
    exe.write_bytes(b"print(1)\n")
    exe.chmod(0o755)
    git(source, "add", "second/script.py")
    git(source, "update-index", "--chmod=+x", "second/script.py")
    git(source, "commit", "-qm", "executable")
    repo = source.parent / "linked"
    git(source, "worktree", "add", "--detach", str(repo), "HEAD")
    plan["evidence"]["revision"] = git(repo, "rev-parse", "HEAD")
    def change_script(root):
        (root / "second/script.py").write_bytes(b"print(2)\n")
        return AgentResult(0, "수정했습니다")
    execute(repo, plan, Worker(repo, change_script), [])
    assert (repo / "second/script.py").read_text() == "print(2)\n"
    assert (source / "second/script.py").read_text() == "print(1)\n"
    assert "mode change" not in git(repo, "diff", "--summary", "--", "second/script.py")
    if os.name != "nt":
        assert os.access(repo / "second/script.py", os.X_OK)


@pytest.mark.parametrize("failure", ["exit", "scope", "dirty", "link", "question"])
def test_failure_leaves_all_primary_edits_intact(setup, failure):
    repo, plan = setup
    logs = []

    def mutate(root):
        if failure == "exit":
            return AgentResult(1, "provider failed")
        if failure == "scope":
            (root / "first/extra.py").write_text("wrong scope")
        if failure == "dirty":
            (repo / "second/old.py").write_text("manual edit")
        if failure == "link":
            (root / "second/linked.py").hardlink_to(root / "first/old.py")
        if failure == "question":
            return AgentResult(0, "AGENT_OFFICE_PRODUCT_DECISION\n표시할까요?")
        return AgentResult(0, "done")

    with pytest.raises(ValueError, match="병렬"):
        execute(repo, plan, Worker(repo, mutate), logs)
    assert (repo / "first/old.py").read_bytes() == b"x = 1\n"
    assert (repo / "second/old.py").read_text() == ("manual edit" if failure == "dirty" else "x = 1\n")
    assert not list(repo.rglob("new.py"))
    assert not any("주 작업 트리에 반영" in text for _, text in logs)


@pytest.mark.parametrize("scopes", [["."], ["first/child"], ["FIRST"], []])
def test_overlap_or_missing_scope_preserves_sequential_workflow(setup, scopes):
    repo, plan = setup
    plan["tasks"][1]["folders"] = scopes
    logs = []
    assert execute(repo, plan, None, logs) == ""
    assert logs == []

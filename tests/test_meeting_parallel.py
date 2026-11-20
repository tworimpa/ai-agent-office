import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agent_office.adapters import AgentResult
from agent_office.meeting_parallel import execute_parallel
from agent_office.studio_inbox import Inbox, InboxChannel
from agent_office.studio_runner import LocalRunner
from test_studio_inbox import seed


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

    def execute(self, prompt, *, workspace, allow_edits, strict_completion, inbox):
        assert allow_edits and strict_completion
        assert inbox is not None
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
                            record=lambda *row: logs.append(row), open_inbox=Mock)


def test_parallel_permissions_use_distinct_live_conversations(setup):
    repo, plan = setup
    path, run_id = repo.parent / "studio.sqlite3", uuid4().hex
    with closing(LocalRunner(path)):
        pass
    seed(path, run_id)
    inbox = Inbox(path)
    channels = []

    def open_inbox():
        channel = InboxChannel(path, run_id, "developer", "cline")
        channels.append(channel)
        return channel

    class RequestingWorker:
        def execute(self, prompt, *, workspace, allow_edits, strict_completion, inbox):
            item = json.loads(prompt.splitlines()[-1])["assigned"]
            target = workspace / item["folders"][0] / "new.py"
            decision = asyncio.run(asyncio.wait_for(inbox.ask("permission", str(target), item["id"]), 10))
            with inbox.sending(decision["id"]):
                if decision["decision"] == "approve":
                    target.write_text("x = 2")
            inbox.delivered(decision["id"])
            return AgentResult(0, "완료")

    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(execute_parallel, plan, repo, {"cline": RequestingWorker()}, "cline",
                          task="요청", record=lambda *args: None, open_inbox=open_inbox)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not job.done():
            requests = inbox.pending()
            if len(requests) == 2:
                break
            time.sleep(.02)
        else:
            pytest.fail("Both independent permission requests must arrive")
        for row in requests:
            action = "approve" if Path(row["prompt"]).parent.name == "first" else "reject"
            inbox.decide(run_id, row["id"], {"action": action})
        with pytest.raises(ValueError, match="병렬"):
            job.result(timeout=10)
    assert len({channel.holder for channel in channels}) == 2 and all(channel.closed for channel in channels)
    assert {row["decision"] for row in inbox.history(run_id) if row["status"] == "delivered"} == {"approve", "reject"}
    assert not list(repo.rglob("new.py"))


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


def test_goal_finishes_disjoint_work_while_product_choice_waits(setup):
    from agent_office import goal_recovery, meeting_store, overnight_goals
    from agent_office.batch_dialogue import run_batch_dialogue
    from agent_office.dialogue import PRODUCT_DECISION
    from agent_office.meeting_runtime import RoomMeeting
    from agent_office.store import TaskStore
    from agent_office.workflow import build_graph

    repo, plan = setup
    path, run_id = repo.parent / "goal.sqlite3", uuid4().hex
    (repo/"test_join.py").write_text("from pathlib import Path\ndef test_join():\n"
                                    "    assert Path('first/joined.txt').read_text() == 'dark/done'\n")
    git(repo, "add", "test_join.py")
    git(repo, "commit", "-qm", "test dependent task")
    plan["evidence"]["revision"] = git(repo,"rev-parse","HEAD")
    script = repo.parent / "batch_fixture.py"
    script.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "message=json.load(sys.stdin)['message']['content']\n"
        "if '--conversation' in sys.argv:\n"
        "    assert message.endswith('dark')\n"
        "    with Path('first/new.py').open('a') as f: f.write('dark')\n"
        "    reply='done'\n"
        "else: reply='AGENT_OFFICE_PRODUCT_DECISION\\nChoose a display mode.'\n"
        "print(json.dumps({'status':'SUCCESS','response':reply,'conversation_id':'12345678-1234-4234-a234-123456789abc'}))\n")
    with closing(LocalRunner(path)):
        pass
    seed(path, run_id)
    with TaskStore(path) as store:
        overnight_goals.ensure_schema(store.connection)
        store.connection.execute("UPDATE studio_runs SET folders='[\".\"]' WHERE id=?", (run_id,))
        store.connection.execute("UPDATE tasks SET developer_engine='antigravity' WHERE current_run_id=?", (run_id,))
        overnight_goals.register(store.connection, run_id, "workspace", "fixture", plan["evidence"]["revision"],
                                 ["."], {"goal_criteria":["check"], "goal_policy":{}})
        meeting_store.register(store.connection,run_id)
        meeting_store.propose(store.connection,run_id,plan)
    inbox = Inbox(path)
    worker_timeout = 4
    class WorkerWithChoice:
        def execute(self, prompt, *, workspace, allow_edits, strict_completion=False, inbox=None):
            if prompt.startswith("MEETING_IMPLEMENT"):
                item = json.loads(prompt.splitlines()[-1])["assigned"]
                if item["id"] == "A":
                    return run_batch_dialogue("antigravity",[sys.executable,str(script)],prompt,workspace,
                                              os.environ.copy(),worker_timeout,inbox)
                (workspace/"second/new.py").write_text("done")
            elif allow_edits:
                remaining = json.loads(prompt.splitlines()[-1])["remaining"]
                assert [task["id"] for task in remaining] == ["C"]
                value = (workspace/"first/new.py").read_text()+"/"+(workspace/"second/new.py").read_text()
                (workspace/"first/joined.txt").write_text(value)
            else:
                return AgentResult(0,"VERDICT: APPROVED")
            return AgentResult(0, "완료")
    adapters = {"antigravity":WorkerWithChoice(), "gemini":WorkerWithChoice()}
    meeting = RoomMeeting(path,run_id,repo,"fixture","antigravity","gemini",["."],adapters)
    meeting.begin = lambda: None  # The approved plan is already in the meeting journal.
    graph = build_graph(adapters,meeting=meeting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(graph.invoke,dict(workspace=str(repo), task="fixture",developer="antigravity",reviewer="gemini",
                                            attempts=0,max_attempts=1,allow_edits=True,test_argv=[sys.executable,"-m","pytest","-q"]))
        for _ in range(600):
            if (repo/"second/new.py").exists() or job.done():
                break
            time.sleep(.03)
        if job.done():
            pytest.fail(str(job.result()))
        assert (repo / "second/new.py").read_text() == "done"
        assert not (repo / "first/new.py").exists()
        for _ in range(100):
            if decisions := inbox.pending():
                break
            time.sleep(.02)
        decision, = decisions
        assert decision["kind"] == PRODUCT_DECISION
        with TaskStore(path) as store:
            assert goal_recovery.timing(store.connection, run_id)["waiting_for_user"]
        time.sleep(worker_timeout+.3)
        assert not job.done() and not (repo/"first/joined.txt").exists()
        inbox.decide(run_id, decision["id"], {"action":"answer", "answer":"dark"})
        assert job.result(timeout=20)["status"] == "READY_FOR_HUMAN_REVIEW"
    assert (repo / "first/new.py").read_text() == "dark"
    assert (repo/"first/joined.txt").read_text() == "dark/done"
    history, = inbox.history(run_id)
    assert history["status"] == "delivered"


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

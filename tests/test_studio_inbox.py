import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import platform
import subprocess
import sys
import time
import uuid

import pytest

from agent_office.models import OfficeError, OwnerInfo, TaskSpec
from agent_office.store import TaskStore
from agent_office.studio_inbox import Inbox, InboxChannel, expire_requests
from agent_office.studio_runner import LocalRunner
from agent_office.workspace import Worktree


def seed(path, run_id, *, interactive=True):
    with TaskStore(path) as store:
        def register(db):
            db.execute("INSERT INTO studio_runs(id,workspace_id,folders,allow_edits,request_hash) "
                       "VALUES (?,'workspace','[]',1,'digest')", (run_id,))
            if interactive:
                db.execute("INSERT INTO studio_interactive_runs VALUES (?)", (run_id,))

        store.create_run(TaskSpec(uuid.uuid4().hex, "project", "task", "cline", "gemini", 2), run_id,
                         Worktree(path.parent, "fixture"), OwnerInfo(platform.node(), uuid.uuid4().hex),
                         register=register)
        store.record_transition(run_id, operation_key="start", event_type="STAGE_STARTED",
                                state="RUNNING", attempts=1)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / "저널.sqlite3"
    run_id = uuid.uuid4().hex
    with closing(LocalRunner(path)):
        pass
    seed(path, run_id)
    monkeypatch.setattr("agent_office.studio_inbox.POLL_SECONDS", 0.005)
    inbox = Inbox(path)
    channel = InboxChannel(path, run_id, "developer", "cline")
    yield path, run_id, inbox, channel
    channel.expire()


async def pending(channel, inbox, kind="permission", prompt="파일을 읽을까요?"):
    task = asyncio.create_task(channel.ask(kind, prompt, "live-session"))
    await asyncio.sleep(0)
    row = next(row for row in inbox.pending() if row["prompt"] == prompt)
    return task, row["id"]


def test_concurrent_human_decisions_and_terminal_receipt_retries(journal):
    path, run_id, inbox, channel = journal

    async def exercise():
        waiter, request_id = await pending(channel, inbox)
        with pytest.raises(ValueError):
            inbox.decide(run_id, request_id, {"action": "answer", "answer": "yes"})

        def submit(action):
            try:
                return Inbox(path).decide(run_id, request_id, {"action": action})
            except OfficeError as error:
                return error.code

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(submit, ["approve", "reject"]))
        assert results.count("REQUEST_CONFLICT") == 1
        decided = next(row for row in results if isinstance(row, dict))
        assert decided == await waiter
        assert decided["status"] == "decided" and decided["delivered_at"] is None
        assert inbox.decide(run_id, request_id, {"action": decided["decision"]}) == decided

        other = InboxChannel(path, run_id, "developer", "cline")
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"), other.sending(request_id):
            pytest.fail("a different session attempted delivery")
        with channel.sending(request_id):
            pass
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"), channel.sending(request_id):
            pytest.fail("one-time grant sent twice")
        channel.delivered(request_id)
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
            channel.delivered(request_id)
        channel.expire()
        with TaskStore(path, existing=True) as store:
            store.record_transition(run_id, operation_key="done", event_type="RUN_FINISHED",
                                    state="PLAN_ONLY", outcome="PLAN_ONLY")
        receipt = Inbox(path).decide(run_id, request_id, {"action": decided["decision"]})
        assert receipt == inbox.history(run_id)[0]
        assert receipt["status"] == "delivered"
        assert receipt["created_at"] <= receipt["decided_at"] <= receipt["delivered_at"]
        assert not inbox.pending()
        opposite = "approve" if decided["decision"] == "reject" else "reject"
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            inbox.decide(run_id, request_id, {"action": opposite})

    asyncio.run(exercise())


def test_stop_and_expiry_preserve_unconfirmed_decision_in_one_transaction(journal):
    path, run_id, inbox, channel = journal

    async def exercise():
        waiter, request_id = await pending(channel, inbox, "question")
        payload = {"action": "answer", "answer": "첫 줄\n  둘째 줄  "}
        assert inbox.decide(run_id, request_id, payload)["answer"] == payload["answer"]
        await waiter
        with TaskStore(path, existing=True) as store:
            with pytest.raises(RuntimeError), store._write():
                expire_requests(store.connection, run_id)
                raise RuntimeError()
            assert inbox.history(run_id)[0]["status"] == "decided"
            with store._write():
                store.connection.execute("UPDATE studio_runs SET stop_requested=1 WHERE id=?", (run_id,))
                expire_requests(store.connection, run_id)
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"), channel.sending(request_id):
            pytest.fail("response was sent after stop")
        receipt = inbox.decide(run_id, request_id, payload)
        assert receipt["status"] == "expired" and receipt["delivered_at"] is None
        assert receipt["answer"] == payload["answer"] and receipt["expired_at"] >= receipt["decided_at"]
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            inbox.decide(run_id, request_id, dict(payload, answer="changed"))

    asyncio.run(exercise())


def test_actor_run_host_and_session_are_bound(journal):
    path, run_id, inbox, channel = journal
    other_run = uuid.uuid4().hex
    seed(path, other_run, interactive=False)

    async def exercise():
        waiter, request_id = await pending(channel, inbox, "question")
        with pytest.raises(OfficeError, match="NOT_FOUND"):
            inbox.decide(other_run, request_id, {"action": "answer", "answer": "text"})
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
            await channel.ask("question", "another", "different-session")
        for run, role, engine in [(run_id, "reviewer", "gemini"), (run_id, "developer", "gemini"),
                                  (other_run, "developer", "cline")]:
            other = InboxChannel(path, run, role, engine)
            try:
                with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
                    await other.ask("question", "mismatch", "other")
            finally:
                other.expire()
        with TaskStore(path, existing=True) as store:
            store.connection.execute("UPDATE runs SET host_name=? WHERE run_id=?", ("another-host", run_id))
            assert inbox.pending() == []
            with pytest.raises(OfficeError, match="NOT_FOUND"):
                inbox.history(run_id)
            with pytest.raises(OfficeError, match="NOT_FOUND"):
                inbox.decide(run_id, request_id, {"action": "answer", "answer": "text"})
            store.connection.execute("UPDATE runs SET host_name=?,attempts=2 WHERE run_id=?",
                                     (platform.node(), run_id))
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
            inbox.decide(run_id, request_id, {"action": "answer", "answer": "stale"})
        assert inbox.history(run_id)[0]["status"] == "expired"
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
            await waiter

    asyncio.run(exercise())


def test_question_and_permission_validation_and_timeout(journal):
    path, run_id, inbox, channel = journal

    async def exercise():
        for unsafe in ("TOKEN=fixture_secret", "값" * 3000, " "):
            with pytest.raises(ValueError):
                await channel.ask("permission", unsafe, "live-session")
        assert inbox.history(run_id) == []
        waiter, request_id = await pending(channel, inbox, "question")
        for payload in ({"action": "approve"}, {"action": "answer", "answer": "비" * 3000},
                        {"action": "answer", "answer": "TOKEN=fixture_secret"},
                        {"action": "answer", "answer": "   "},
                        {"action": "answer", "answer": "ok", "extra": True}):
            with pytest.raises(ValueError):
                inbox.decide(run_id, request_id, payload)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(waiter, 0.015)
        assert inbox.pending() == []
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
            inbox.decide(run_id, request_id, {"action": "answer", "answer": "late"})
        assert inbox.history(run_id)[0]["decision"] is None
        assert b"fixture_secret" not in path.read_bytes()

    asyncio.run(exercise())


def test_oldest_pending_survives_many_newer_requests(journal, monkeypatch):
    _, run_id, inbox, channel = journal
    monkeypatch.setattr("agent_office.studio_inbox.LIST_LIMIT", 2)

    async def exercise():
        tasks = [asyncio.create_task(channel.ask("question", str(i), "same")) for i in range(4)]
        await asyncio.sleep(0)
        assert [row["prompt"] for row in inbox.pending()] == ["0", "1"]
        request = inbox.pending()[0]
        inbox.decide(run_id, request["id"], {"action": "answer", "answer": "ok"})
        assert [row["prompt"] for row in inbox.pending()] == ["1", "2"]
        channel.expire()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())


def test_worker_crash_expires_request_before_supervisor_recovery(journal):
    path, run_id, inbox, _ = journal
    code = ("import asyncio,sys; from agent_office.studio_inbox import InboxChannel; "
            "c=InboxChannel(sys.argv[1],sys.argv[2],'developer','cline'); "
            "asyncio.run(c.ask('question','visible','fixture'))")
    with subprocess.Popen([sys.executable, "-c", code, str(path), run_id]) as child:
        try:
            deadline = time.monotonic() + 12
            while not (rows := inbox.pending()):
                assert child.poll() is None and time.monotonic() < deadline
                time.sleep(0.02)
            request_id = rows[0]["id"]
        finally:
            child.kill()
            child.wait(timeout=10)
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
        inbox.decide(run_id, request_id, {"action": "answer", "answer": "late"})
    assert inbox.history(run_id)[0]["status"] == "expired"
    with TaskStore(path, readonly=True) as reader:
        assert reader.get_run(run_id).state == "RUNNING"

def test_unacknowledged_pipe_write_cannot_be_replayed_after_session_exit(journal):
    path, run_id, inbox, channel = journal

    async def exercise():
        waiter, request_id = await pending(channel, inbox)
        inbox.decide(run_id, request_id, {"action": "approve"})
        await waiter
        with channel.sending(request_id):
            pass
        channel.expire()  # Process exited after write, before the ACP receipt.
        assert inbox.history(run_id)[0]["delivered_at"] is None
        receipt = inbox.decide(run_id, request_id, {"action": "approve"})
        assert (receipt["status"], receipt["decision"]) == ("expired", "approve")

        replacement = InboxChannel(path, run_id, "developer", "cline")
        try:
            task, new_id = await pending(replacement, inbox, prompt="retry attempt")
            assert new_id != request_id
            with pytest.raises(OfficeError, match="REQUEST_CLOSED"), replacement.sending(request_id):
                pytest.fail("new session reused unconfirmed permission")
            assert not task.done() and inbox.pending()[0]["decision"] is None
        finally:
            replacement.expire()
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
            await task

    asyncio.run(exercise())


def test_generic_journal_and_observer_do_not_create_inbox(tmp_path):
    path = tmp_path / "generic.sqlite3"
    with TaskStore(path):
        pass
    with TaskStore(path, readonly=True) as observer:
        assert not observer.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='studio_requests'").fetchone()
    with closing(LocalRunner(path)) as runner:
        assert not runner.inbox.pending()
    with TaskStore(path, readonly=True) as observer:
        assert observer.connection.execute("PRAGMA user_version").fetchone()[0] == 1

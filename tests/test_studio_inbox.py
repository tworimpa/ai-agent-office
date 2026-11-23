import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
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


def test_product_decision_is_typed_and_cannot_use_delegated_technical_answer(journal, monkeypatch):
    from agent_office import away_ledger
    from agent_office.away_expert import RULES
    from agent_office.office_state import office_snapshot
    from agent_office.office_wire import check_office_reply
    from agent_office.studio_ssh import _check_inbox_reply

    path, run_id, inbox, channel = journal
    with TaskStore(path, existing=True) as store, store._write():
        away_ledger.register_policy(store.connection, run_id, {
            "rules": [{"topic": "test_command", "path": "README.md"}],
            "specialist_engine": "codex", "verifier_engine": "antigravity",
        }, "a" * 40)
    monkeypatch.setattr("agent_office.away_consult.run_consultation",
                        lambda *a, **k: pytest.fail("product decisions must never consult"))

    async def exercise():
        waiter, request_id = await pending(channel, inbox, "product_decision", RULES["test_command"])
        item, = inbox.pending()
        assert item["kind"] == "product_decision"
        assert item["source"] is None and not item["delegation_in_progress"]
        _check_inbox_reply(200, {"requests": [item]}, "GET", "/inbox", None)
        assert away_ledger.return_report(path)["counts"]["product_decisions"] == 1
        office = office_snapshot(path)
        assert office["counts"]["product_decisions"] == 1 and office["counts"]["questions"] == 0
        check_office_reply(office, 0)
        with pytest.raises(ValueError):
            inbox.decide(run_id, request_id, {"action": "approve"})
        payload = {"action": "answer", "answer": "기존 입력 흐름을 유지합니다."}
        decided = Inbox(path).decide(run_id, request_id, payload)
        assert await waiter == decided
        assert decided["source"] == "operator"
        with channel.sending(request_id):
            pass
        channel.delivered(request_id)
        history, = Inbox(path).history(run_id)
        assert history["kind"] == "product_decision" and history["status"] == "delivered"

    asyncio.run(exercise())


def test_dialogue_product_decision_marker_has_its_own_continuation():
    from agent_office.dialogue import continuation_prompt, extract_request

    marker = "AGENT_OFFICE_PRODUCT_DECISION"
    assert extract_request(marker + "\r\n사용자 동작을 바꿀까요?\r\n") == (
        "product_decision", "사용자 동작을 바꿀까요?")
    assert extract_request("AGENT_OFFICE_QUESTION\n테스트 명령?") == ("question", "테스트 명령?")
    assert "product behavior" in continuation_prompt("keep", kind="product_decision")
    with pytest.raises(ValueError):
        extract_request(marker + "\nAGENT_OFFICE_QUESTION\n질문")


def test_choice_only_policy_records_guarded_answer_and_rejects_other_actions(journal, monkeypatch):
    from agent_office import away_ledger
    from agent_office.studio_ssh import RemoteUnavailable, _check_inbox_reply, _check_return_report
    from agent_office.technical_choices import CHOICE_MARKER, answer_for_option, render_choice

    path, run_id, inbox, channel = journal
    revision = "a" * 40
    choice = {"purpose": "diagnosis", "options": [{"path": "src/worker.py", "line": 9},
                                                 {"path": "src/other.py", "line": 10}]}
    evidence = {"purpose": "diagnosis", "option": 1, "path": "src/worker.py", "line": 9,
                "quote": "def worker():", "revision": revision}
    calls = []

    async def consult(repo, revision, rule, specialist, verifier, folders, **kwargs):
        calls.append(rule)
        assert (specialist, verifier, folders) == ("codex", "codex", ["src"])
        return {"answer": answer_for_option(evidence), "evidence": evidence}

    monkeypatch.setattr("agent_office.away_consult.run_consultation", consult)
    with TaskStore(path, existing=True) as store, store._write():
        store.connection.execute("UPDATE studio_runs SET folders=? WHERE id=?", ('["src"]', run_id))
        away_ledger.register_choices(store.connection, run_id, revision, ("codex", "codex"))
    assert away_ledger.get_policy(path, run_id) is None and away_ledger.get_choices(path, run_id)

    async def exercise():
        waiter = asyncio.create_task(channel.ask("question", CHOICE_MARKER + "\n" + json.dumps(choice), "live-session"))
        proposal = await asyncio.wait_for(waiter, 3)
        assert proposal["prompt"] == render_choice(choice) and proposal["source"] == "delegate"
        with channel.sending(proposal["id"]):
            pass
        channel.delivered(proposal["id"])
        item, = inbox.history(run_id)
        assert item["delegation_details"]["evidence"] == evidence
        _check_inbox_reply(200, {"requests": [item]}, "GET", f"/runs/{run_id}/requests", None)
        report = away_ledger.return_report(path)
        assert report["counts"]["active"] == report["counts"]["delegated"] == 1
        _check_return_report(report)
        tampered = dict(report["delegated"][0], evidence=dict(evidence, option=2))
        with pytest.raises(RemoteUnavailable):
            _check_return_report(dict(report, delegated=[tampered]))

        for invalid in (dict(choice, action="approve"), dict(choice, purpose="product")):
            text = CHOICE_MARKER + "\n" + json.dumps(invalid)
            answer_waiter, request_id = await pending(channel, inbox, "question", text)
            item, = inbox.pending()
            assert item["delegation_reason"] == away_ledger.INVALID_CHOICE
            with pytest.raises(ValueError):
                inbox.decide(run_id, request_id, {"action": "approve"})
            inbox.decide(run_id, request_id, {"action": "answer", "answer": "운영자 결정"})
            assert (await answer_waiter)["source"] == "operator"
        assert len(calls) == 1

    asyncio.run(exercise())
    away_ledger.revoke_policy(path, run_id)
    assert not away_ledger.get_choices(path, run_id)
    assert away_ledger.return_report(path)["counts"]["active"] == 0


def test_new_inbox_type_migrates_existing_questions_with_away_history(journal):
    path, run_id, inbox, channel = journal

    async def exercise():
        waiter, request_id = await pending(channel, inbox, "question")
        payload = {"action": "answer", "answer": "ok"}
        inbox.decide(run_id, request_id, payload)
        await waiter
        with channel.sending(request_id):
            pass
        channel.delivered(request_id)

        with TaskStore(path, existing=True) as store, store._write():
            db = store.connection
            db.execute("INSERT INTO studio_away_checks(request_id,reason) VALUES (?,?)",
                       (request_id, "existing diagnostic"))
            db.execute("ALTER TABLE studio_requests DROP COLUMN decision_type")
        row, = Inbox(path).history(run_id)
        assert row["id"] == request_id and row["kind"] == "question" and row["status"] == "delivered"
        assert row["delegation_reason"] == "existing diagnostic"

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

# The provider pair is exercised separately with mock CLI executables. These
# tests control the consultation timing to cover the journal's race boundaries.
@pytest.fixture
def away(journal):
    from agent_office.away_expert import RULES
    from agent_office.away_ledger import register_policy

    path, run_id, _, _ = journal
    policy = {"rules": [{"topic": "test_command", "path": "docs/README.md"}],
              "specialist_engine": "codex", "verifier_engine": "gemini"}
    revision = "a" * 40
    with TaskStore(path, existing=True) as store, store._write():
        store.connection.execute("UPDATE studio_runs SET folders=? WHERE id=?", ('["docs"]', run_id))
        register_policy(store.connection, run_id, policy, revision)
    return policy, revision, RULES["test_command"]


def test_away_answer_and_evidence_survive_delivery_completion_and_reopen(journal, away, monkeypatch):
    from agent_office.away_ledger import get_policy, get_revision, return_report, revoke_policy
    from agent_office.role_skills import RoleSkillRegistry, pin_selection

    path, run_id, inbox, channel = journal
    policy, revision, prompt = away
    evidence = {"path": "docs/README.md", "line": 6, "quote": "pytest -q", "revision": revision}
    calls = []
    registry = RoleSkillRegistry(path)
    old = registry.create("문서 근거", "EXACT_QUOTE")
    verifier_skill = registry.create("검토", "COMPARE_EVIDENCE")
    developer_skill = registry.create("작업자 전용", "WORKER_ONLY")
    with TaskStore(path) as store, store._write():
        pin_selection(store.connection, run_id, {"specialist": [old["id"]], "verifier": [verifier_skill["id"]],
                                                 "developer": [developer_skill["id"]]})
    registry.create("문서 근거", "UPDATED_QUOTE")

    async def consult(repo, rev, rule, specialist, verifier, folders, *, skill_guidance, on_stage, db_path):
        assert set(skill_guidance) == {"specialist", "verifier"}
        assert "EXACT_QUOTE" in skill_guidance["specialist"] and "COMPARE_EVIDENCE" not in skill_guidance["specialist"]
        assert "COMPARE_EVIDENCE" in skill_guidance["verifier"] and "EXACT_QUOTE" not in skill_guidance["verifier"]
        assert all("WORKER_ONLY" not in text and "UPDATED_QUOTE" not in text for text in skill_guidance.values())
        calls.append((repo, rev, rule, specialist, verifier, folders))
        return {"answer": "pytest -q", "evidence": evidence}

    monkeypatch.setattr("agent_office.away_consult.run_consultation", consult)
    assert get_policy(path, run_id) == policy
    assert return_report(path)["active"] == [{"run_id": run_id, "task": "task"}]
    row = asyncio.run(asyncio.wait_for(channel.ask("question", prompt, "session"), timeout=5))
    assert calls == [(path.parent, revision, policy["rules"][0], "codex", "gemini", ["docs"])]
    assert row["source"] == "delegate" and row["answer"] == "pytest -q"
    assert inbox.history(run_id)[0]["status"] == "pending"
    assert not return_report(path)["delegated"]
    with channel.sending(row["id"]):
        pass
    # Transport accepted the answer before revocation. Its receipt stays valid.
    revoke_policy(path, run_id)
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"), channel.sending(row["id"]):
        pytest.fail("a claimed answer was replayed")
    channel.delivered(row["id"])
    channel.expire()
    with TaskStore(path, existing=True) as store:
        store.record_transition(run_id, operation_key="done", event_type="RUN_FINISHED",
                                state="PLAN_ONLY", outcome="PLAN_ONLY")

    with closing(LocalRunner(path)):
        report = return_report(path)
        assert report["counts"] == {"active": 0, "delegated": 1, "questions": 0, "permissions": 0}
        assert not report["active"]
        receipt, = report["delegated"]
        assert receipt["id"] == row["id"] and receipt["status"] == "delivered"
        assert receipt["run_state"] == receipt["outcome"] == "PLAN_ONLY"
        assert receipt["evidence"] == evidence and receipt["topic"] == "test_command"
        assert receipt["specialist_engine"] == "codex" and receipt["verifier_engine"] == "gemini"
        revoke_policy(path, run_id)
        assert get_policy(path, run_id) is None and get_revision(path, run_id) == revision
        assert return_report(path)["delegated"] == [receipt]


@pytest.mark.parametrize("intervene", ["operator", "revoke"])
def test_completed_proposal_is_pending_until_original_session_writes_it(journal, away, monkeypatch, intervene):
    from agent_office.away_ledger import return_report, revoke_policy
    from agent_office.dialogue import DelegationWithdrawn

    path, run_id, inbox, channel = journal
    _, revision, prompt = away

    async def exercise():
        async def provider(*args, skill_guidance, on_stage, db_path):
            return {"answer": "pytest", "evidence": {"path": "docs/README.md", "line": 1,
                                                    "quote": "pytest", "revision": revision}}

        monkeypatch.setattr("agent_office.away_consult.run_consultation", provider)
        proposal = await asyncio.wait_for(channel.ask("question", prompt, "session"), timeout=5)
        row, = inbox.pending()
        assert row["id"] == proposal["id"] and row["decision"] is None and not return_report(path)["delegated"]
        human = {"action": "answer", "answer": "사람의 답"}
        if intervene == "revoke":
            revoke_policy(path, run_id)
        else:
            inbox.decide(run_id, row["id"], human)
        with pytest.raises(DelegationWithdrawn), channel.sending(row["id"]):
            pytest.fail("a superseded proposal reached the transport")
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"), channel.sending(row["id"]):
            pytest.fail("a superseded proposal was replayed")
        waiter = asyncio.create_task(channel.await_operator(row["id"]))
        await asyncio.sleep(0)
        if intervene == "revoke":
            assert not waiter.done() and inbox.pending()[0]["id"] == row["id"]
            inbox.decide(run_id, row["id"], human)
        reply = await asyncio.wait_for(waiter, timeout=5)
        assert (reply["id"], reply["answer"], reply["source"]) == (row["id"], "사람의 답", "operator")
        with channel.sending(reply["id"]):
            pass
        channel.delivered(reply["id"])
        assert not return_report(path)["delegated"] and inbox.history(run_id)[0]["status"] == "delivered"

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["withdrawn_wait_canceled", "write_unconfirmed"])
def test_unsent_or_unconfirmed_proposal_never_replays(journal, away, monkeypatch, failure):
    from agent_office.away_ledger import return_report, revoke_policy
    from agent_office.dialogue import DelegationWithdrawn

    path, run_id, inbox, channel = journal
    _, revision, prompt = away

    async def consult(*args, skill_guidance, on_stage, db_path):
        return {"answer": "pytest", "evidence": {"path": "docs/README.md", "line": 1,
                                                "quote": "pytest", "revision": revision}}

    monkeypatch.setattr("agent_office.away_consult.run_consultation", consult)

    async def exercise():
        proposal = await asyncio.wait_for(channel.ask("question", prompt, "session"), timeout=5)
        request_id = proposal["id"]
        if failure == "withdrawn_wait_canceled":
            revoke_policy(path, run_id)
            with pytest.raises(DelegationWithdrawn), channel.sending(request_id):
                pytest.fail("revoked proposal was sent")
            waiter = asyncio.create_task(channel.await_operator(request_id))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not return_report(path)["delegated"]
        else:
            with pytest.raises(OSError, match="write unconfirmed"), channel.sending(request_id):
                raise OSError("write unconfirmed")
            receipt, = return_report(path)["delegated"]
            assert receipt["answer"] == "pytest" and receipt["status"] == "expired"
        row, = inbox.history(run_id)
        assert row["status"] == "expired" and row["delivered_at"] is None
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"), channel.sending(request_id):
            pytest.fail("closed request was replayed")

    asyncio.run(exercise())


@pytest.mark.parametrize("competing", ["operator", "revoke", "stop", "attempt", "scope", "prompt", "policy"])
def test_away_inflight_decision_rechecks_current_snapshot(journal, away, monkeypatch, competing):
    from agent_office.away_ledger import return_report, revoke_policy

    path, run_id, inbox, channel = journal
    policy, revision, prompt = away

    async def exercise():
        entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def consult(*args, skill_guidance, on_stage, db_path):
            entered.set()
            try:
                await release.wait()
                return {"answer": "pytest -q", "evidence": {"path": "docs/README.md", "line": 6,
                                                            "quote": "pytest -q", "revision": revision}}
            finally:
                exited.set()
        monkeypatch.setattr("agent_office.away_consult.run_consultation", consult)
        waiter = asyncio.create_task(channel.ask("question", prompt, "session"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        request, = inbox.pending()
        if competing == "operator":
            inbox.decide(run_id, request["id"], {"action": "answer", "answer": "사람의 답"})
        elif competing == "revoke":
            assert revoke_policy(path, run_id) == revoke_policy(path, run_id) == {"run_id": run_id, "active": False}
            # The worker must tear down even a consultation that never returns.
            await asyncio.wait_for(exited.wait(), timeout=5)
        else:
            with TaskStore(path, existing=True) as store, store._write():
                db = store.connection
                if competing == "stop":
                    db.execute("UPDATE studio_runs SET stop_requested=1 WHERE id=?", (run_id,))
                    expire_requests(db, run_id)
                elif competing == "attempt":
                    db.execute("UPDATE runs SET attempts=2 WHERE run_id=?", (run_id,))
                elif competing == "scope":
                    db.execute("UPDATE studio_runs SET folders=? WHERE id=?", ('["src"]', run_id))
                elif competing == "prompt":
                    db.execute("UPDATE studio_requests SET prompt=? WHERE id=?", ("다른 질문", request["id"]))
                elif competing == "policy":
                    changed = dict(policy, verifier_engine="antigravity")
                    db.execute("UPDATE studio_away_policies SET policy_json=? WHERE run_id=?",
                               (json.dumps(changed), run_id))
        release.set()
        await asyncio.wait_for(exited.wait(), timeout=5)
        if competing in {"stop", "attempt"}:
            with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
                await asyncio.wait_for(waiter, timeout=5)
            assert inbox.history(run_id)[0]["status"] == "expired"
        else:
            if competing != "operator":
                pending_row, = inbox.pending()
                assert pending_row["delegation_reason"] and pending_row["source"] is None
                inbox.decide(run_id, request["id"], {"action": "answer", "answer": "사람의 답"})
            reply = await asyncio.wait_for(waiter, timeout=5)
            assert reply["answer"] == "사람의 답" and reply["source"] == "operator"
        assert not return_report(path)["delegated"]

    asyncio.run(exercise())


def test_unmatched_permission_and_provider_failure_stay_in_operator_inbox(journal, away, monkeypatch):
    from agent_office.away_evidence import DelegationDeferred
    from agent_office.away_ledger import return_report

    path, run_id, inbox, channel = journal
    _, _, prompt = away

    async def exercise():
        calls = []
        async def consult(*args, skill_guidance, on_stage, db_path):
            calls.append(args)
            raise DelegationDeferred("evidence_unavailable")

        monkeypatch.setattr("agent_office.away_consult.run_consultation", consult)
        tasks = [asyncio.create_task(channel.ask(kind, question, "session")) for kind, question in [
            ("permission", prompt), ("question", prompt + " "), ("question", prompt)]]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        report = return_report(path)
        assert report["counts"] == {"active": 1, "questions": 2, "permissions": 1, "delegated": 0}
        assert len(calls) == 1
        with TaskStore(path, readonly=True) as store:
            assert not store.connection.execute("SELECT 1 FROM studio_away_role_stages").fetchall()
        for row in Inbox(path).pending():
            if row["kind"] == "permission":
                assert row["delegation_reason"] is None
                action = {"action": "reject"}
            else:
                assert row["delegation_reason"]
                action = {"action": "answer", "answer": "운영자"}
            inbox.decide(run_id, row["id"], action)
        replies = await asyncio.gather(*tasks)
        assert all(reply["source"] == "operator" for reply in replies)
        assert len(calls) == 1

    asyncio.run(exercise())


def test_report_window_retains_full_history_and_omits_stale_or_foreign_rows(journal, away, monkeypatch):
    from agent_office import away_ledger

    path, run_id, inbox, channel = journal
    policy, revision, prompt = away
    foreign = uuid.uuid4().hex
    seed(path, foreign)
    with TaskStore(path, existing=True) as store, store._write():
        away_ledger.register_policy(store.connection, foreign, policy, revision)
        store.connection.execute("UPDATE runs SET host_name='other-host' WHERE run_id=?", (foreign,))
    monkeypatch.setattr(away_ledger, "REPORT_LIMIT", 1)
    evidence = {"path": "docs/README.md", "line": 1, "quote": "pytest", "revision": revision}

    async def consult(*args, skill_guidance, on_stage, db_path):
        return {"answer": "pytest", "evidence": evidence}

    monkeypatch.setattr("agent_office.away_consult.run_consultation", consult)

    async def exercise():
        for _ in range(2):
            reply = await asyncio.wait_for(channel.ask("question", prompt, "session"), timeout=5)
            with channel.sending(reply["id"]):
                pass
            channel.delivered(reply["id"])
        question = asyncio.create_task(channel.ask("question", "새로운 질문", "session"))
        permission = asyncio.create_task(channel.ask("permission", prompt, "session"))
        await asyncio.sleep(0)
        with TaskStore(path, existing=True) as store, store._write():
            # Simulate an abandoned channel without first opening the inbox.
            store.connection.execute("UPDATE studio_requests SET holder=? WHERE kind='permission'", (uuid.uuid4().hex,))
        report = away_ledger.return_report(path)
        assert report["limit"] == len(report["delegated"]) == 1
        assert report["counts"] == {"delegated": 2, "active": 1, "questions": 1, "permissions": 0}
        history = inbox.history(run_id)
        assert report["delegated"][0]["id"] == history[1]["id"]
        assert history[0]["delegation_details"] == {"topic": "test_command", "evidence": evidence,
                                                     "specialist_engine": "codex", "verifier_engine": "gemini"}
        assert history[3]["status"] == "expired"
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
            await permission
        inbox.decide(run_id, history[2]["id"], {"action": "answer", "answer": "운영자 답변"})
        await question
        with pytest.raises(OfficeError, match="NOT_FOUND"):
            away_ledger.revoke_policy(path, foreign)

    asyncio.run(exercise())

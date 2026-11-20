from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import asyncio
import os
import platform
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from agent_office.models import OfficeError, OwnerInfo, TaskSpec
from agent_office import batch_dialogue, studio_directions
from agent_office.adapters import CLIAdapter
from agent_office.ownership import RunGuard
from agent_office.store import TaskStore
from agent_office.studio_directions import DirectionChannel, Directions
from agent_office.studio_runner import LocalRunner
from agent_office.workspace import Worktree

from test_acp_adapters import Answers, run_fixture
import test_batch_dialogue as batch
import test_codex_inbox as codex
from test_studio_runner import repo, wait_for


@pytest.fixture
def direction_run(tmp_path):
    path = tmp_path / "저널.sqlite3"
    run_id = uuid.uuid4().hex
    with closing(LocalRunner(path)):
        pass

    def register(db):
        db.execute("INSERT INTO studio_runs(id,workspace_id,folders,allow_edits,request_hash) "
                   "VALUES (?,'workspace','[]',1,'digest')", (run_id,))
        db.execute("INSERT INTO studio_interactive_runs VALUES (?)", (run_id,))
        db.execute("INSERT INTO studio_room_assignments VALUES (?,'{}','fixture')", (run_id,))

    with TaskStore(path, existing=True) as store:
        store.create_run(TaskSpec(uuid.uuid4().hex, "project", "task", "cline", "gemini", 2), run_id,
                         Worktree(path.parent, "fixture"), OwnerInfo(platform.node(), uuid.uuid4().hex),
                         register=register)
        store.record_transition(run_id, operation_key="start", event_type="STAGE_STARTED",
                                state="RUNNING", attempts=1)
    with RunGuard(path, run_id):
        yield path, run_id


def payload(message="다음 확인 사항입니다.", role="developer", attempt=1):
    return {"id": uuid.uuid4().hex, "role": role, "attempt": attempt, "message": message}


def test_concurrent_submissions_are_idempotent_scoped_and_ordered(direction_run):
    path, run_id = direction_run
    store = Directions(path)
    request = payload("추가 범위를 확인하세요.\n  원문 유지  ")
    with ThreadPoolExecutor(2) as executor:
        rows = list(executor.map(lambda _: store.submit(run_id, request), range(2)))
    assert rows[0] == rows[1] == {**request, "created_at": rows[0]["created_at"], "delivery": "queued"}
    with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
        store.submit(run_id, {**request, "message": "다른 지시"})
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
        store.submit(run_id, payload(role="reviewer"))
    with pytest.raises(OfficeError, match="NOT_FOUND"):
        store.submit(uuid.uuid4().hex, request)
    with pytest.raises(ValueError):
        store.submit(run_id, payload("API_KEY=private"))
    store.submit(run_id, payload("다음 지시"))
    channel = DirectionChannel(path, run_id, "developer", "cline")
    delivery = channel.next()
    assert delivery.meeting_seconds == 0
    assert "추가 범위를 확인하세요." in delivery.prompt and "다음 지시" in delivery.prompt
    assert delivery.prompt.index("추가 범위를 확인하세요.") < delivery.prompt.index("다음 지시")
    assert [row["delivery"] for row in store.history(run_id)] == ["sending", "sending"]
    with delivery.sending():
        pass
    delivery.delivered()
    assert [row["delivery"] for row in store.history(run_id)] == ["delivered", "delivered"]
    assert channel.next() is None
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
        store.submit(run_id, payload())
    assert store.submit(run_id, request)["delivery"] == "delivered"


def test_stop_during_meeting_still_closes_delivery(direction_run, monkeypatch):
    path, run_id = direction_run
    directions = Directions(path)
    channel = DirectionChannel(path, run_id, "developer", "cline")
    directions.submit(run_id, payload())
    times = iter((5.0, 95.0))
    monkeypatch.setattr(studio_directions, "time", SimpleNamespace(monotonic=lambda: next(times)))

    def stop_while_waiting(rows):
        with TaskStore(path, existing=True) as store, store._write():
            store.connection.execute("UPDATE studio_runs SET stop_requested=1 WHERE id=?", (run_id,))
        return "기존 계획"

    channel.replan = stop_while_waiting
    delivery = channel.next()
    assert delivery.meeting_seconds == 90
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"), delivery.sending():
        pytest.fail("Stopped direction reached the provider")


def test_uncertain_closed_attempt_cannot_replay(direction_run):
    path, run_id = direction_run
    store = Directions(path)
    channel = DirectionChannel(path, run_id, "developer", "cline")
    first = store.submit(run_id, payload("sending"))
    delivery = channel.next()
    with pytest.raises(OSError), delivery.sending():
        raise OSError("lost connection during write")
    second = store.submit(run_id, payload("queued"))
    channel.close()
    assert store.history(run_id) == [{**first, "delivery": "uncertain"}, {**second, "delivery": "undelivered"}]
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
        DirectionChannel(path, run_id, "developer", "cline")
    with TaskStore(path, existing=True) as task_store:
        task_store.record_transition(run_id, operation_key="test", event_type="STAGE_STARTED",
                                     state="TESTING", attempts=1)
        task_store.record_transition(run_id, operation_key="retry", event_type="RETRY_SCHEDULED",
                                     state="RUNNING", attempts=1)
        task_store.record_transition(run_id, operation_key="new", event_type="STAGE_STARTED",
                                     state="RUNNING", attempts=2)
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
        store.submit(run_id, payload("전 단계에서 늦게 도착한 지시", attempt=1))
    assert store.submit(run_id, payload("현재 단계에서 보낸 지시", attempt=2))["delivery"] == "queued"
    current = DirectionChannel(path, run_id, "developer", "cline")
    assert "현재 단계에서 보낸 지시" in current.next().prompt
    current.close()
    assert [row["delivery"] for row in store.history(run_id)] == ["uncertain", "undelivered", "uncertain"]


def test_history_preserves_retries_after_terminal_without_new_directions(direction_run):
    path, run_id = direction_run
    store = Directions(path)
    request = payload()
    before = store.submit(run_id, request)
    with TaskStore(path, existing=True) as task_store:
        task_store.record_transition(run_id, operation_key="done", event_type="RUN_FINISHED",
                                     state="PLAN_ONLY", outcome="PLAN_ONLY")
    after = {**before, "delivery": "undelivered"}
    assert Directions(path).history(run_id) == [after]
    assert store.submit(run_id, request) == after
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
        store.submit(run_id, payload())


def test_directions_reject_foreign_host_and_do_not_bypass_pending_permission(direction_run):
    path, run_id = direction_run
    store = Directions(path)
    request = payload()
    channel = DirectionChannel(path, run_id, "developer", "cline")
    row = store.submit(run_id, request)
    with TaskStore(path, existing=True) as task_store:
        db = task_store.connection
        db.execute("UPDATE runs SET host_name='foreign-host' WHERE run_id=?", (run_id,))
        with pytest.raises(OfficeError, match="NOT_FOUND"):
            store.history(run_id)
        db.execute("UPDATE runs SET host_name=? WHERE run_id=?", (platform.node(), run_id))
        db.execute("""INSERT INTO studio_requests(id,run_id,role,engine,attempt,holder,session_hash,kind,prompt,status,created_at)
                      VALUES (?,?,'developer','cline',1,'holder','session-hash','permission','patch','pending',?)""",
                   (uuid.uuid4().hex, run_id, row["created_at"]))
    delivery = channel.next()
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"), delivery.sending():
        pytest.fail("Direction could reach a worker with a pending permission")
    assert store.history(run_id) == [{**row, "delivery": "undelivered"}]


def test_legacy_studio_run_exposes_empty_history_and_refuses_new_directions(direction_run):
    path, run_id = direction_run
    with TaskStore(path, existing=True) as store:
        store.connection.execute("DELETE FROM studio_room_assignments WHERE run_id=?", (run_id,))
    store = Directions(path)
    assert store.history(run_id) == []
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
        store.submit(run_id, payload())


def test_last_turn_races_with_submission_without_leaving_queued_work(direction_run):
    path, run_id = direction_run
    store = Directions(path)
    channel = DirectionChannel(path, run_id, "developer", "cline")

    def submit():
        try:
            return store.submit(run_id, payload())
        except OfficeError as error:
            assert error.code == "REQUEST_CLOSED"
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        submission = executor.submit(submit)
        boundary = executor.submit(channel.next)
        row, delivery = submission.result(), boundary.result()
    if row is None:
        assert delivery is None and not store.history(run_id)
    else:
        assert delivery is not None
        with delivery.sending():
            pass
        delivery.delivered()
        assert channel.next() is None
        assert store.history(run_id) == [{**row, "delivery": "delivered"}]


def test_reviewer_directions_are_scoped_to_review_and_unavailable_sessions_close(direction_run):
    path, run_id = direction_run
    store = Directions(path)
    developer = store.submit(run_id, payload("개발 지시"))
    with TaskStore(path, existing=True) as task_store:
        task_store.record_transition(run_id, operation_key="test", event_type="STAGE_STARTED", state="TESTING")
        task_store.record_transition(run_id, operation_key="review", event_type="STAGE_STARTED", state="REVIEWING")
    with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
        store.submit(run_id, payload())
    reviewer = store.submit(run_id, payload("검토 지시", role="reviewer"))
    channel = DirectionChannel(path, run_id, "reviewer", "gemini")
    assert channel.next(available=False) is None
    assert store.history(run_id) == [{**row, "delivery": "undelivered"} for row in (developer, reviewer)]


def test_local_runner_delivers_direction_after_answer_in_same_codex_thread(tmp_path, repo, monkeypatch):
    codex.install_mock_codex(tmp_path, monkeypatch, "direction-plan")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    path = tmp_path / "run.sqlite3"
    run = uuid.uuid4().hex
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    with closing(LocalRunner(path)) as runner:
        runner.start(run, uuid.uuid4().hex, repo, ["."], "원래 작업", False, use_inbox=True,
                     room_assignment={}, room_revision=revision)
        wait_for(lambda: runner.inbox.pending() or runner.detail(run)["state"] not in ("QUEUED", "RUNNING"))
        pending = runner.inbox.pending()
        assert pending and pending[0]["run_id"] == run, runner.detail(run)
        store = Directions(path)
        row = store.submit(run, payload('중간 지시 & "원문"'))
        assert store.history(run) == [row] and row["delivery"] == "queued"
        runner.inbox.decide(run, pending[0]["id"], {"action": "answer", "answer": "둘째 방식"})
        wait_for(lambda: runner.detail(run)["state"] not in ("QUEUED", "RUNNING"))
        result = runner.detail(run)
        assert (result["state"], result["developer_output"]) == ("PLAN_ONLY", "지시 반영")
        assert store.history(run) == [{**row, "delivery": "delivered"}]


class FixtureDirections:
    prompt = '중간 지시 & "원문"'
    meeting_seconds = 0.0

    def __init__(self, check_boundary):
        self.check_boundary = check_boundary
        self.claimed = self.sent = self.confirmed = 0

    def next(self, *, available=True):
        self.check_boundary()
        assert available
        self.claimed += 1
        return self if self.claimed == 1 else None

    @contextmanager
    def sending(self):
        assert self.sent == 0
        self.sent += 1
        yield

    def delivered(self):
        assert self.sent == 1
        self.confirmed += 1


@pytest.mark.parametrize("engine", ["codex", "cline", "claude"])
@pytest.mark.parametrize("meeting_seconds", [3600, 0])
def test_only_server_meeting_time_is_excluded_from_provider_deadline(tmp_path, monkeypatch, engine, meeting_seconds):
    def advance_clock():
        if directions.claimed:
            return
        # Move the provider clock one hour across the synchronous handoff.
        # Only the server-measured meeting interval is eligible for exclusion.
        if engine == "claude":
            monkeypatch.setattr(batch_dialogue, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + 3600))
        else:
            loop = asyncio.get_running_loop()
            original_time = loop.time
            monkeypatch.setattr(loop, "time", lambda: original_time() + 3600)

    directions = FixtureDirections(advance_clock)
    directions.meeting_seconds = meeting_seconds
    if engine == "codex":
        operator = codex.Operator()
        operator.directions = directions
        _, result = codex.fixture(tmp_path, monkeypatch, "direction", operator=operator)
    elif engine == "cline":
        inbox = Answers()
        inbox.directions = directions
        result = run_fixture(tmp_path, monkeypatch, engine, "inbox_direction", edit=True, inbox=inbox)
    else:
        adapter = CLIAdapter(engine, timeout=15)
        original = adapter.command("", allow_edits=False)[1:]
        command = [sys.executable, batch.__file__, engine, str(uuid.uuid4()), "direction", *original]
        monkeypatch.setattr(adapter, "command", lambda *args, **kwargs: command)
        operator = batch.Operator()
        operator.directions = directions
        result = adapter.execute("Original specification", workspace=tmp_path, allow_edits=False, inbox=operator)
    assert result.returncode == (0 if meeting_seconds else 124)
    assert directions.confirmed == (1 if meeting_seconds else 0)


@pytest.mark.parametrize("case", ["direction", "direction-lost"])
def test_codex_direction_runs_after_approval_in_same_thread(tmp_path, monkeypatch, case):
    operator = codex.Operator()
    operator.directions = FixtureDirections(lambda: operator.confirmed == ["1", "2"] or pytest.fail("Approval pending"))
    _, result = codex.fixture(tmp_path, monkeypatch, case, operator=operator)
    assert operator.directions.sent == 1
    if case == "direction":
        assert (result.returncode, result.session_id, result.message, operator.directions.confirmed) == (0, "thread", "지시 반영", 1)
    else:
        assert result.returncode and operator.directions.confirmed == 0


@pytest.mark.parametrize("case", ["inbox_direction", "inbox_direction_lost"])
@pytest.mark.parametrize("engine", ["cline", "gemini"])
def test_acp_direction_runs_after_questions_and_permissions(tmp_path, monkeypatch, engine, case):
    inbox = Answers()
    receipts = [1, 2] if engine == "cline" else [1]
    inbox.directions = FixtureDirections(lambda: sorted(inbox.receipts) == receipts or pytest.fail("Operator request pending"))
    result = run_fixture(tmp_path, monkeypatch, engine, case, edit=True, inbox=inbox)
    assert inbox.directions.sent == 1
    if case == "inbox_direction":
        assert (result.returncode, result.session_id, result.message, inbox.directions.confirmed) == (0, "fixture", "지시 반영", 1)
    else:
        assert result.returncode and inbox.directions.confirmed == 0


@pytest.mark.parametrize("engine", ["claude", "antigravity"])
@pytest.mark.parametrize("case", ["direction", "direction-lost"])
def test_batch_direction_resumes_named_session_and_rejects_wrong_session(tmp_path, monkeypatch, engine, case):
    session = str(uuid.uuid4())
    adapter = CLIAdapter(engine, timeout=15)
    original = adapter.command("", allow_edits=False)[1:]
    monkeypatch.setattr(adapter, "command", lambda *args, **kwargs: [sys.executable, batch.__file__, engine, session, case, *original])
    operator = batch.Operator()
    operator.directions = FixtureDirections(lambda: operator.confirmed == ["question-1"] or pytest.fail("Answer pending"))
    result = adapter.execute("Original specification", workspace=tmp_path, allow_edits=False, inbox=operator)
    assert operator.directions.sent == 1
    if case == "direction":
        assert (result.returncode, result.session_id, result.message, operator.directions.confirmed) == (0, session, "지시 반영", 1)
    else:
        assert result.returncode and operator.directions.confirmed == 0

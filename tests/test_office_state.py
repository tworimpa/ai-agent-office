"""Exercise the read-only projection against the Studio journal and real holder locks."""
from contextlib import closing
import json
import platform
import uuid

import pytest

from agent_office import team_runs
from agent_office.models import OwnerInfo, TaskSpec
from agent_office.office_state import PAGE_SIZE, office_snapshot
from agent_office.ownership import RunGuard
from agent_office.store import TaskStore, now
from agent_office.studio_inbox import Inbox
from agent_office.studio_runner import LocalRunner
from agent_office.workspace import Worktree
from test_studio_runner import install_fake, repo, wait_for


@pytest.fixture
def journal(tmp_path):
    path = tmp_path / "오피스.sqlite3"
    with closing(LocalRunner(path)):
        pass
    with TaskStore(path, existing=True) as store:
        yield store


def new_run(store, *, edits=True, host=None, task="원래 요청"):
    run_id = uuid.uuid4().hex
    spec = TaskSpec(uuid.uuid4().hex, "private/project", task, "codex", "codex", 2)
    def register(db):
        db.execute("""INSERT INTO studio_runs(id,workspace_id,folders,allow_edits,request_hash,diff)
                      VALUES (?,'workspace','[]',?,'digest','private diff')""", (run_id, edits))
        db.execute("INSERT INTO studio_interactive_runs VALUES (?)", (run_id,))
    store.create_run(spec, run_id, Worktree(store.path.parent / "private-dir", "private-branch"),
                     OwnerInfo(host or platform.node(), uuid.uuid4().hex), register=register)
    return run_id


def event(store, run, event_type, *, role=None, state=None, outcome=None, attempt=None):
    payload = {"role": role} if role else {}
    if role in ("developer", "reviewer"):
        payload["engine"] = "codex"
    store.record_transition(run, operation_key=uuid.uuid4().hex, event_type=event_type, state=state,
                            outcome=outcome, attempts=attempt, payload=payload)


def request(store, run, holder, kind="question", prompt="질문", role="developer", engine="codex", attempt=1):
    request_id = uuid.uuid4().hex
    store.connection.execute("""INSERT INTO studio_requests
        (id,run_id,role,engine,attempt,holder,session_hash,kind,prompt,status,created_at)
        VALUES (?,?,?,?,?,?,'private-session',?,?,'pending',?)""",
        (request_id, run, role, engine, attempt, holder, kind, prompt, now()))
    return request_id


def activities(store, run):
    return {card["role"]: card["activity"] for card in office_snapshot(store.path)["agents"]
            if card["run_id"] == run}


def test_owning_host_child_identity_and_live_stage_transitions(journal):
    run = new_run(journal)
    plan = new_run(journal, edits=False)
    new_run(journal, host="unrelated-host")
    parent = uuid.uuid4().hex
    db = journal.connection
    team_runs.register(db, parent, "digest", "workspace", "상위 작업 TOKEN=private123", [0],
                       ["."], True, "codex", "codex", True, None, "rev")
    db.execute("INSERT INTO studio_team_children VALUES (?,?,0,'[0]')", (run, parent))
    snap = office_snapshot(journal.path)
    assert snap["total_runs"] == snap["counts"]["active_runs"] == 2
    assert {c["run_id"] for c in snap["agents"]} == {run, plan}
    assert activities(journal, plan) == {"developer": "queued"}
    cards = [c for c in snap["agents"] if c["run_id"] == run]
    assert len(cards) == 2 and all(c["parent_id"] == parent and c["engine"] == "codex" for c in cards)
    assert all(c["parent_task"] == "상위 작업 TOKEN=[redacted]" for c in cards)
    assert "private" not in json.dumps(snap)
    event(journal, run, "STAGE_STARTED", role="developer", state="RUNNING", attempt=1)
    assert activities(journal, run) == {"developer": "working", "reviewer": "queued"}
    event(journal, run, "STAGE_COMPLETED", role="developer")
    assert activities(journal, run)["developer"] == "done"  # Run state still says RUNNING.
    event(journal, run, "STAGE_STARTED", role="tester", state="TESTING")
    assert activities(journal, run) == {"developer": "testing", "reviewer": "queued"}
    event(journal, run, "STAGE_COMPLETED", role="tester")
    event(journal, run, "STAGE_STARTED", role="reviewer", state="REVIEWING")
    assert activities(journal, run) == {"developer": "done", "reviewer": "working"}
    event(journal, run, "STAGE_COMPLETED", role="reviewer")
    event(journal, run, "RETRY_SCHEDULED", role="reviewer", state="RUNNING")
    assert activities(journal, run) == {"developer": "queued", "reviewer": "queued"}
    event(journal, run, "STAGE_STARTED", role="developer", attempt=2)
    event(journal, run, "STAGE_FAILED", role="developer")
    event(journal, run, "RUN_FINISHED", state="FAILED", outcome="DEVELOPER_FAILED")
    assert activities(journal, run) == {"developer": "failed", "reviewer": "skipped"}


@pytest.mark.parametrize("terminal,activity", [("CANCELED", "stopped"), ("RECOVERY_REQUIRED", "recovery")])
def test_stop_recovery_and_finished_plan(journal, terminal, activity):
    run = new_run(journal)
    event(journal, run, "STAGE_STARTED", role="developer", state="RUNNING", attempt=1)
    journal.connection.execute("UPDATE studio_runs SET stop_requested=1 WHERE id=?", (run,))
    assert activities(journal, run) == {"developer": "stopping", "reviewer": "skipped"}
    event(journal, run, "RUN_FINISHED", state=terminal, outcome="CANCELED" if terminal=="CANCELED" else "INTERRUPTED")
    assert activities(journal, run) == {"developer": activity, "reviewer": "skipped"}
    plan = new_run(journal, edits=False)
    event(journal, plan, "STAGE_STARTED", role="developer", state="RUNNING", attempt=1)
    event(journal, plan, "STAGE_COMPLETED", role="developer")
    event(journal, plan, "RUN_FINISHED", state="PLAN_ONLY", outcome="PLAN_ONLY")
    assert activities(journal, plan) == {"developer": "done"}


def test_read_only_request_binding_and_live_holder(journal):
    run = new_run(journal, task="나"*800)
    event(journal, run, "STAGE_STARTED", role="developer", state="RUNNING", attempt=1)
    holder = uuid.uuid4().hex
    with RunGuard(journal.path, holder):
        first = request(journal, run, holder, prompt="질"*640)
        request(journal, run, holder, "permission", "쓰기 허용?")
        for _ in range(3):
            request(journal, run, holder)
        for stale in (dict(role="reviewer"), dict(engine="gemini"), dict(attempt=0)):
            request(journal, run, holder, **stale)
        missing = uuid.uuid4().hex
        request(journal, run, missing)
        snap = office_snapshot(journal.path)
        assert snap["counts"] == {"active_runs": 1, "questions": 4, "permissions": 1}
        dev = snap["agents"][0]
        assert dev["activity"] == "waiting" and dev["request_count"] == 5
        assert [len(dev["task"]), len(dev["requests"]), len(dev["requests"][0]["prompt"])] == [320, 3, 240]
        assert dev["requests"][0]["id"] == first and dev["requests"][1]["kind"] == "permission"
        assert not RunGuard(journal.path, missing).path.exists()
        Inbox(journal.path).decide(run, first, {"action":"answer", "answer":"답변"})
        assert office_snapshot(journal.path)["counts"]["questions"] == 3
    assert office_snapshot(journal.path)["counts"] == {"active_runs": 1, "questions": 0, "permissions": 0}
    assert activities(journal, run)["developer"] == "working"
    assert journal.connection.execute("SELECT count(*) FROM studio_requests WHERE status='pending'").fetchone()[0] == 8


def test_delegate_in_progress_history_and_no_phantom_verifier(journal):
    db = journal.connection
    run = new_run(journal)
    event(journal, run, "STAGE_STARTED", role="developer", state="RUNNING", attempt=1)
    db.execute("INSERT INTO studio_away_policies VALUES (?,'{}','revision',NULL)", (run,))
    holder = uuid.uuid4().hex
    with RunGuard(journal.path, holder):
        q = request(journal, run, holder)
        db.execute("INSERT INTO studio_away_checks(request_id) VALUES (?)", (q,))
        assert activities(journal, run) == {"developer": "checking", "reviewer": "queued"}
        db.execute("INSERT INTO studio_away_role_stages VALUES (?,'specialist','cline','RUNNING',?)", (q, now()))
        assert activities(journal, run)["specialist"] == "working"
        assert office_snapshot(journal.path)["counts"]["questions"] == 0
        db.execute("UPDATE studio_away_role_stages SET status='COMPLETED'")
        db.execute("INSERT INTO studio_away_role_stages VALUES (?,'verifier','gemini','RUNNING',?)", (q, now()))
        assert activities(journal, run)["verifier"] == "working"
        db.execute("UPDATE studio_away_policies SET revoked_at=?", (now(),))
        assert activities(journal, run) == {"developer": "waiting", "reviewer": "queued",
                                            "specialist": "done", "verifier": "stopped"}
        assert office_snapshot(journal.path)["counts"]["questions"] == 1
    # A pre-telemetry, successfully delegated receipt proves both engines completed.
    db.execute("DELETE FROM studio_away_role_stages")
    db.execute("UPDATE studio_away_checks SET evidence='[]',specialist_engine='cline',verifier_engine='gemini'")
    db.execute("UPDATE studio_requests SET status='delivered',decided_at=?,decision='answer'", (now(),))
    assert activities(journal, run) == {"developer": "working", "reviewer": "queued",
                                        "specialist": "done", "verifier": "done"}


def test_paged_snapshot_prioritizes_human_requests_and_bounds_history(journal):
    older = new_run(journal, edits=False)
    event(journal, older, "STAGE_STARTED", role="developer", state="RUNNING", attempt=1)
    for _ in range(PAGE_SIZE):
        new_run(journal, edits=False)
    terminal = []
    for _ in range(12):
        run = new_run(journal)
        event(journal, run, "RUN_FINISHED", state="CANCELED", outcome="CANCELED")
        terminal.append(run)
    holder = uuid.uuid4().hex
    with RunGuard(journal.path, holder):
        request(journal, older, holder)
        first = office_snapshot(journal.path)
        second = office_snapshot(journal.path, first["next_offset"])
        assert first["agents"][0]["run_id"] == older
        assert first["total_runs"] == second["total_runs"] == 33
        assert first["counts"] == second["counts"] == {"active_runs":25,"questions":1,"permissions":0}
        assert first["next_offset"] == 24 and second["next_offset"] is None
        assert len(first["agents"]) == 24
        assert len(second["agents"]) == 17
        assert {a["run_id"] for a in second["agents"] if a["run_state"] == "CANCELED"} == set(terminal[-8:])
        past = office_snapshot(journal.path, 99)
        assert past["counts"] == first["counts"] and past["agents"] == [] and past["next_offset"] is None
    for offset in (-1, True, 2**63):
        with pytest.raises(ValueError):
            office_snapshot(journal.path, offset)


def test_stages_and_counts_share_one_read_transaction(journal, monkeypatch):
    from agent_office import office_state

    journal.connection.execute("PRAGMA journal_mode=WAL")
    run = new_run(journal, edits=False)
    event(journal, run, "STAGE_STARTED", role="developer", state="RUNNING", attempt=1)
    read_stages = office_state._latest_stages

    def finish_between_snapshot_queries(db, run_ids):
        event(journal, run, "STAGE_COMPLETED", role="developer")
        event(journal, run, "RUN_FINISHED", state="PLAN_ONLY", outcome="PLAN_ONLY")
        return read_stages(db, run_ids)

    with monkeypatch.context() as patch:
        patch.setattr(office_state, "_latest_stages", finish_between_snapshot_queries)
        snapshot = office_snapshot(journal.path)
    assert snapshot["counts"]["active_runs"] == 1 and snapshot["agents"][0]["activity"] == "working"
    assert activities(journal, run) == {"developer": "done"}


def test_mock_cli_stage_visible_until_worker_finishes(tmp_path, repo, monkeypatch):
    gate, marker = tmp_path / "finish", tmp_path / "started"
    install_fake(tmp_path, monkeypatch,
        "import pathlib, sys, time\n"
        "sys.stdin.read()\n"
        f"pathlib.Path({str(marker)!r}).touch()\n"
        f"while not pathlib.Path({str(gate)!r}).exists(): time.sleep(.02)\n"
        "print('fixture plan')\n")
    path = tmp_path / "run.sqlite3"
    with closing(LocalRunner(path)) as runner:
        run = uuid.uuid4().hex
        runner.start(run, uuid.uuid4().hex, repo, ["."], "계획", False)
        try:
            wait_for(marker.exists)
            assert office_snapshot(path)["agents"][0]["activity"] == "working"
        finally:
            gate.touch()
        wait_for(lambda: office_snapshot(path)["agents"][0]["run_state"] == "PLAN_ONLY")
        assert office_snapshot(path)["agents"][0]["activity"] == "done"


def test_concurrent_probes_cannot_supply_liveness_for_a_dead_channel(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import os
    import threading

    guard = RunGuard(tmp_path / "journal", uuid.uuid4().hex)
    assert not guard.is_held() and not guard.path.parent.exists()
    with guard:
        assert guard.is_held()
    if os.name == "nt":
        import win32file as lock_module
        lock_name = "LockFileEx"
    else:
        import fcntl as lock_module
        lock_name = "flock"
    lock = getattr(lock_module, lock_name)
    acquired, release = threading.Event(), threading.Event()

    def hold_first_probe(*args):
        result = lock(*args)
        if not acquired.is_set():
            acquired.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(lock_module, lock_name, hold_first_probe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(guard.is_held)
        try:
            assert acquired.wait(10)
            assert not guard.is_held()
        finally:
            release.set()
        assert not first.result()

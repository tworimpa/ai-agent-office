from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_office import goal_recovery, goal_stage_wait as gates
from agent_office.adapters import AgentResult
from agent_office.goal_verification import default_policy
from agent_office.studio_runner import LocalRunner, finish_drained
from agent_office.workflow import build_graph
from test_goal_recovery import goal
from test_workflow import MockAdapter, base_state


def test_drained_wait_survives_studio_restart_and_never_replays_uncertain_work(goal, monkeypatch):
    from agent_office import workspace_coordination as coordination
    runner, store, run_id, _ = goal
    db = store.connection
    result = {"ready": False, "reasons": ["waiting on source"], "decisions": []}
    monkeypatch.setattr(coordination, "try_stage", lambda *_: result)
    monkeypatch.setattr(coordination, "stage_status", lambda *_: result)
    monkeypatch.setattr(coordination, "priority", lambda *_: 0)
    state = {"status": "NEW", "attempts": 0, "feedback": "measured regression"}
    with pytest.raises(gates.Parked):
        gates.enter(store, run_id, "implement", "develop", state, 1)
    finish_drained(store, run_id, worker_succeeded=True, preview_finished=False)
    assert store.get_run(run_id).ended_at is None
    assert goal_recovery.detail(store, run_id)["disposition"] == "waiting_dependency"
    assert goal_recovery.timing(db, run_id)["waiting_for_user"]
    runner.close()
    calls = []
    monkeypatch.setattr(LocalRunner, "_start_monitor", lambda _: None)
    monkeypatch.setattr(LocalRunner, "_launch", lambda _, store, ident, prompt: calls.append(ident))
    with closing(LocalRunner(store.path)) as restarted:
        restarted._schedule(store)
        assert calls == []
        result.update(ready=True, reasons=[], decisions=[{"request_id": "product", "answer": "convert"}])
        restarted._schedule(store)
        restarted._schedule(store)
        assert calls == [run_id]
        assert goal_recovery.resume_state(db, run_id) == dict(state, next_stage="develop")
        assert gates.enter(store, run_id, "implement", "develop", state, 1) == result["decisions"]
        # Simulate worker loss after an implementation call could have begun.
        finish_drained(store, run_id, worker_succeeded=False, preview_finished=False)
        assert goal_recovery.detail(store, run_id)["disposition"] == "inspect"
        restarted._schedule(store)
        assert calls == [run_id]


def test_owner_schedules_only_ready_goals_in_priority_order(goal, monkeypatch):
    from agent_office import workspace_coordination as coordination
    runner, store, existing, _ = goal
    db = store.connection
    repo = store.get_task(store.get_run(existing).task_id).project
    base = db.execute("SELECT base_commit FROM studio_goals WHERE id=?", (existing,)).fetchone()[0]
    monkeypatch.setattr(runner, "_start_monitor", lambda: None)
    calls = []
    monkeypatch.setattr(runner, "_launch", lambda _, ident, task: calls.append(task))
    ids = [uuid4().hex for _ in range(4)]
    priorities = dict(zip(ids, [9, -2, 7, -2]))
    blocked = {ids[0]}
    monkeypatch.setattr(coordination, "priority", lambda db, ident: priorities[ident])
    monkeypatch.setattr(coordination, "stage_status", lambda db, ident, *args: {
        "ready": ident not in blocked, "reasons": ["pending"] if ident in blocked else []})
    for ident, task in zip(ids, ["blocked", "first low", "ready high", "second low"]):
        runner.start(ident, "room", repo, ["."], task, True, room_revision=base,
                     room_assignment={"goal_policy": default_policy(["test_fixture.py"]), "goal_criteria": ["verify"]})
    runner._schedule(store)
    assert calls == ["ready high", "first low", "second low"]
    blocked.clear()
    runner._schedule(store)
    assert calls == ["ready high", "first low", "second low", "blocked"]


def test_coordinated_readonly_admission_survives_restart_with_exact_evidence(goal, monkeypatch):
    runner, store, source, _ = goal
    db = store.connection
    project = store.get_task(store.get_run(source).task_id).project
    base = db.execute("SELECT base_commit FROM studio_goals WHERE id=?", (source,)).fetchone()[0]
    target, redacted, question = (uuid4().hex for _ in range(3))
    db.execute("""INSERT INTO studio_requests(id,run_id,role,engine,attempt,holder,session_hash,kind,prompt,status,
        created_at,decision_type) VALUES(?,?,'developer','codex',1,'fixture','fixture','question','Mode?',
        'pending',CURRENT_TIMESTAMP,'product_decision')""", (question, source))
    spec = {"priority": 5, "areas": ["config"], "dependencies": [{"source_run": source, "stage": "start",
        "request_id": question, "condition": "product_decision", "reason": "need operator choice"}]}
    audit = store.path.parent / "readonly-prompt.json"
    fake = store.path.parent / "fixture.py"
    fake.write_text("import sys,json,pathlib\np=sys.stdin.read()\n"
        f"pathlib.Path({str(audit)!r}).write_text(json.dumps([sys.argv[1:],p]),encoding='utf8')\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'inspected'}}))\n",
        encoding="utf8")
    if os.name == "nt":
        (store.path.parent / "codex.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
    else:
        wrapper = store.path.parent / "codex"
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fake))} "$@"\n')
        wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(store.path.parent) + os.pathsep + os.environ["PATH"])
    with runner.lock:
        for ident, task in [(target, "inspect configuration"), (redacted, "inspect TOKEN=canary-marker")]:
            runner.start(ident, "room", project, ["."], task, False, room_assignment={}, room_revision=base,
                         coordination=spec)
        runner._schedule(store)
        assert gates.waiting(db, target)["stage"] == "start" and not audit.exists()
        assert not goal_recovery.is_goal(db, target)
        assert not db.execute("SELECT 1 FROM workspace_stage_evidence WHERE run_id=?", (target,)).fetchone()
        runner.close()
    db.execute("UPDATE studio_requests SET status='delivered',decision='answer',answer='canonical-choice' WHERE id=?",
               (question,))
    store.record_transition(source, operation_key="fixture-done", event_type="RUN_FINISHED",
                            state="PLAN_ONLY", outcome="PLAN_ONLY")
    with closing(LocalRunner(store.path)) as restarted:
        deadline = time.monotonic() + 35
        while not store.get_run(target).ended_at and time.monotonic() < deadline:
            time.sleep(.1)
        assert store.get_run(target).state == "PLAN_ONLY", restarted.detail(target)["diagnostic"]
        assert store.get_run(redacted).state == "RECOVERY_REQUIRED"
    argv, prompt = json.loads(audit.read_text(encoding="utf8"))
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "inspect configuration" in prompt and prompt.count("canonical-choice") == 1
    assert "They grant no tool permissions." in prompt and "canary-marker" not in prompt
    receipts = db.execute("SELECT stage,decisions FROM workspace_stage_evidence WHERE run_id=?", (target,)).fetchall()
    assert [r["stage"] for r in receipts] == ["start"]
    assert json.loads(receipts[0]["decisions"])[0]["request_id"] == question
    assert not db.execute("SELECT 1 FROM workspace_stage_evidence WHERE run_id=?", (redacted,)).fetchone()


def test_readonly_room_intake_keeps_read_roles_and_rejects_unreachable_gates(tmp_path, monkeypatch):
    from agent_office.store import TaskStore
    from test_studio_http import project, request, studio
    from test_workspace_rooms import engine_snapshot, register
    folder, _ = project(tmp_path)
    path = tmp_path / "room.sqlite3"
    with studio(path) as server:
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("codex", "antigravity"))
        monkeypatch.setattr(server.runner, "_start_monitor", lambda: None)
        launched = []
        monkeypatch.setattr(server.runner, "_launch", lambda _, ident, task: launched.append(ident))
        workspace = register(server, [folder])
        url = f"/api/rooms/{workspace}/assignments"
        plain = dict(id=uuid4().hex, message="inspect docs", allow_edits=False, delegation=False)
        status, old = request(server, url, plain)
        assert status == 202 and launched == [plain["id"]]
        gated = dict(plain, id=uuid4().hex, coordination={"priority": 4})
        status, receipt = request(server, url, gated)
        assert status == 202, receipt
        assert receipt["run"]["assignment"] == old["run"]["assignment"]
        assert not receipt["run"]["allow_edits"] and "goal_policy" not in receipt["run"]["assignment"]
        assert launched == [plain["id"]]
        invalid = dict(plain, id=uuid4().hex, coordination={"dependencies": [{
            "source_run": plain["id"], "stage": "review", "condition": "completion", "reason": "scope"}]})
        assert request(server, url, invalid)[0] == 400
        with TaskStore(path) as store:
            server.runner._schedule(store)
            assert not store.connection.execute("SELECT 1 FROM workspace_coordination WHERE run_id=?", (plain["id"],)).fetchone()
            assert not store.connection.execute("SELECT 1 FROM runs WHERE run_id=?", (invalid["id"],)).fetchone()
        assert launched == [plain["id"], gated["id"]]
    # The fixture received the launch handoff and deliberately wrote no result.
    # A new Studio must not use the durable start receipt to duplicate that call.
    with closing(LocalRunner(path)) as reopened:
        assert reopened.detail(gated["id"])["state"] == "RECOVERY_REQUIRED"
        assert gated["id"] not in reopened.queued_goals


def test_real_worker_resumes_after_measured_reproducer_without_rerunning_baseline(goal, monkeypatch):
    from agent_office.goal_checks import _unprivileged
    from agent_office.store import now
    if not _unprivileged():
        pytest.skip("requires an ordinary host account for the supervised pytest subprocess")
    runner, store, source, _ = goal
    db = store.connection
    project = Path(store.get_task(store.get_run(source).task_id).project)
    (project / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (project / "briefing-fixture.txt").write_text("fixture")
    (project / "settings.py").write_text("def valid(data):\n    return True\n")
    (project / "tests").mkdir()
    (project / "tests/test_valid.py").write_text("from settings import valid\ndef test_ok(): assert valid({})\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=a@example.invalid", "commit", "-qm",
                    "reproduction fixture"], cwd=project, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
    fake = Path(__file__).resolve().parents[1] / "desktop/fixtures/goal_worker.py"
    bin_dir = store.path.parent / "fake-cli"
    bin_dir.mkdir()
    for alias in ("codex", "agy"):
        if os.name == "nt":
            (bin_dir / (alias + ".cmd")).write_text(f'@"{sys.executable}" "{fake}" {alias} %*\r\n')
        else:
            executable = bin_dir / alias
            executable.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fake))} {alias} "$@"\n')
            executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    target, question = uuid4().hex, uuid4().hex
    db.execute("""INSERT INTO studio_requests(id,run_id,role,engine,attempt,holder,session_hash,kind,prompt,status,
        created_at,decision_type) VALUES(?,?,'developer','codex',1,'fixture','fixture','question','Convert?',
        'pending',?,'product_decision')""", (question, source, now()))
    spec = {"source_run": source, "request_id": question, "stage": "implement", "condition": "product_decision",
            "reason": "Missing setting conversion decision"}
    runner.start(target, "room", project, ["."], "#42 reproduce then implement", True, use_inbox=True,
        room_revision=base, coordination={"dependencies": [spec]}, room_assignment={"reproduce_required": True,
            "goal_policy": default_policy(["tests/test_valid.py"]), "goal_criteria": ["reject invalid settings"]})

    def await_state(check):
        deadline = time.monotonic() + 90
        while not check() and time.monotonic() < deadline:
            time.sleep(.1)
        assert check(), (store.get_run(target).state, runner.detail(target)["diagnostic"])

    await_state(lambda: gates.drained(db, target))
    def baseline_records():
        return list(map(tuple, db.execute("SELECT position,receipt,cases FROM studio_goal_checks WHERE run_id=? "
                                         "AND attempt=0 ORDER BY position", (target,))))
    before = baseline_records()
    assert len(before) == 3
    # The product source here is journal fixture data. The target uses the real process
    # supervisor, isolated Git checkout, local CLI fixture, pytest and immutable receipts.
    with runner.lock:
        db.execute("UPDATE studio_requests SET status='delivered',decision='answer',answer=?,decided_at=?,delivered_at=? "
                   "WHERE id=?", ("자동 변환합니다", now(), now(), question))
        store.record_transition(source, operation_key="source-test", event_type="STAGE_STARTED", state="TESTING")
        store.record_transition(source, operation_key="source-review", event_type="STAGE_STARTED", state="REVIEWING")
        store.record_transition(source, operation_key="source-ready", event_type="RUN_FINISHED",
                                state="READY_FOR_HUMAN_REVIEW", outcome="APPROVED")
        runner.close()
    with closing(LocalRunner(store.path)):
        await_state(lambda: store.get_run(target).ended_at)
    assert store.get_run(target).state == "READY_FOR_HUMAN_REVIEW"
    assert baseline_records() == before
    assert db.execute("SELECT count(*) FROM studio_requests WHERE run_id=?", (target,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM workspace_stage_evidence WHERE run_id=? AND stage='implement'",
                      (target,)).fetchone()[0] == 1


@pytest.mark.parametrize("correction", ["verify", "review", "review_finished"])
def test_changed_decision_reworks_next_attempt_and_never_approves_stale_candidate(tmp_path, correction):
    recorded = []
    task_goal = SimpleNamespace(needs_reproducer=False, baseline=lambda: (True, "baseline"), invalidate=lambda: None,
        verify=lambda *_: (True, False, False, "all passed"), checkpoint=lambda *_: None, stop=lambda *_: None,
        review=lambda attempt, kind, status, output: recorded.append((attempt, status)) or True)
    developer = MockAdapter([AgentResult(0, "implemented")] * 2)
    reviewer = MockAdapter([AgentResult(0, "VERDICT: APPROVED")] * 2)
    def enter(stage, node, state, attempt):
        if attempt == 1 and stage == correction:
            raise gates.ReworkRequired("decision was corrected")
        return [{"request_id": "product", "answer": f"choice-{attempt}"}] if stage == "implement" else []
    graph = build_graph({"claude": developer, "codex": reviewer}, goal=task_goal, stage_gate=enter,
        review_current=lambda attempt: "decision was corrected" if attempt == 1 and correction == "review_finished" else "")
    result = graph.invoke(base_state(tmp_path))
    assert result["status"] == "READY_FOR_HUMAN_REVIEW" and result["attempts"] == 2
    assert [(p.count(f"choice-{i}"), can_edit) for i,(p,can_edit) in enumerate(developer.calls, 1)] == [(1,True)] * 2
    assert (1, "approved") not in recorded and recorded[-1] == (2, "approved")

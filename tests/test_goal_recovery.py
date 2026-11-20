from contextlib import closing
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from agent_office import goal_recovery as recovery, overnight_goals
from agent_office.models import OfficeError, OwnerInfo, TaskSpec
from agent_office.ownership import RunGuard
from agent_office.store import TaskStore
from agent_office.studio_runner import LocalRunner, finish_drained
from agent_office.workspace import create_worktree, plan_worktree


@pytest.fixture
def goal(tmp_path):
    repo = tmp_path / "fixture"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo).decode().strip()
    git("init", "-q")
    (repo / "test_fixture.py").write_text("def test_one(): assert True\n")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=a@example.org", "commit", "-qm", "base")
    revision = git("rev-parse", "HEAD")
    path, run_id = tmp_path / "studio.db", uuid4().hex
    root = create_worktree(repo, planned=plan_worktree(repo, run_id), revision=revision)
    with closing(LocalRunner(path)) as runner, TaskStore(path) as store:
        db = store.connection
        overnight_goals.ensure_schema(db)
        store.create_run(TaskSpec(uuid4().hex, str(repo), "fixture", "codex", "antigravity", 2),
                         run_id, root, OwnerInfo(platform.node(), uuid4().hex))
        db.execute("INSERT INTO studio_runs(id,workspace_id,folders,allow_edits,request_hash) "
                   "VALUES(?,'room','[\".\"]',1,'digest')", (run_id,))
        overnight_goals.register(db, run_id, "room", "fixture", revision, ["."],
                                 {"goal_criteria":["check"],"goal_policy":{"active_seconds":600}})
        store.record_transition(run_id, operation_key="develop", event_type="STAGE_STARTED", state="RUNNING", attempts=1)
        recovery.checkpoint(db, run_id, "test", dict(status="DEVELOPED", attempts=1, developer_output="done"))
        yield runner, store, run_id, root.path


def test_worker_loss_after_checkpoint_resumes_once_under_original_id(goal, monkeypatch):
    runner, store, run_id, _ = goal
    db = store.connection
    # A coordinator exits without unwinding its worker lock. The supervisor
    # observes exit and commits the result only after its process tree drains.
    crash = ("import os,sys; from agent_office.ownership import RunGuard; "
             "guard=RunGuard.worker(sys.argv[1],sys.argv[2]); guard.__enter__(); os._exit(17)")
    result = subprocess.run([sys.executable, "-c", crash, str(store.path), run_id], check=False)
    assert result.returncode == 17
    finish_drained(store, run_id, worker_succeeded=False, preview_finished=False)
    assert recovery.detail(store, run_id)["disposition"] == "resume_available"
    calls = []
    monkeypatch.setattr(runner, "_launch", lambda _, ident, task: calls.append((ident, task)))
    key = uuid4().hex
    for _ in range(2):
        assert recovery.reconcile(runner, run_id, key)["receipt"]["disposition"] == "resumed"
    assert calls == [(run_id, "fixture")]
    assert recovery.resume_state(db, run_id) == dict(next_stage="test", status="DEVELOPED",
                                                     attempts=1, developer_output="done")
    recovery.invalidate_checkpoint(db, run_id)
    finish_drained(store, run_id, worker_succeeded=False, preview_finished=False)
    assert recovery.detail(store, run_id)["disposition"] == "inspect"


def test_normal_supervisor_resumes_only_the_next_review(goal, monkeypatch):
    from agent_office.goal_candidates import capture
    from agent_office.goal_verification import default_policy, init_schema

    runner, store, run_id, root = goal
    db = store.connection
    metadata = db.execute("SELECT base_commit FROM studio_goals WHERE id=?", (run_id,)).fetchone()
    policy = default_policy(["test_fixture.py"])
    policy["max_attempts"] = 2
    db.execute("UPDATE studio_goals SET policy_json=? WHERE id=?", (json.dumps(policy), run_id))
    db.execute("INSERT INTO studio_room_assignments VALUES(?,?,?)",
               (run_id, json.dumps({"goal_policy":policy}), metadata[0]))
    # A fake reviewer emits the vendor wire format. Any developer dispatch fails the run.
    bin_dir = store.path.parent / "bin"
    bin_dir.mkdir()
    fake = Path(__file__).resolve().parents[1] / "desktop/fixtures/goal_worker.py"
    for alias, engine in (("agy","agy"), ("codex","forbidden")):
        if os.name == "nt":
            (bin_dir / (alias + ".cmd")).write_text(f'@"{sys.executable}" "{fake}" {engine} %*\r\n')
        else:
            command = bin_dir / alias
            command.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fake))} {engine} "$@"\n')
            command.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    (root / "setting.py").write_text("enabled = True\n")
    files, patch, digest = capture(root, metadata[0], ["."])
    init_schema(db)
    candidate = {"attempt":1, "digest":digest, "files":files, "eligible":True, "score":1}
    db.execute("INSERT INTO studio_goal_candidates VALUES(?,?,?,?)", (run_id,1,json.dumps(candidate),patch))
    store.record_transition(run_id, operation_key="verify", event_type="STAGE_STARTED", state="TESTING")
    recovery.checkpoint(db, run_id, "review", dict(attempts=1, status="TEST_PASSED", test_output="1 passed"))
    finish_drained(store, run_id, worker_succeeded=False, preview_finished=False)
    assert recovery.reconcile(runner, run_id, uuid4().hex)["receipt"]["disposition"] == "resumed"
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if store.get_run(run_id).ended_at:
            break
        time.sleep(.1)
    assert store.get_run(run_id).state == "READY_FOR_HUMAN_REVIEW", runner.detail(run_id)["diagnostic"]
    assert root.exists()
    later = db.execute("SELECT event_type FROM events WHERE run_id=? AND seq>(SELECT seq FROM events WHERE "
                       "run_id=? AND event_type='RECOVERY_RESUMED') ORDER BY seq", (run_id, run_id))
    assert [row[0] for row in later] == ["STAGE_STARTED", "STAGE_COMPLETED", "RUN_FINISHED"]


@pytest.mark.parametrize("fault", ["test_changed", "next_stage", "in_flight", "direction", "worker_alive",
                                    "supervisor_lost", "stop", "base_changed", "host_mismatch"])
def test_recovery_refuses_unverified_external_work(goal, fault, monkeypatch):
    runner, store, run_id, root = goal
    db = store.connection
    if fault == "next_stage":
        store.record_transition(run_id, operation_key="test", event_type="STAGE_STARTED", state="TESTING")
    if fault == "supervisor_lost":
        assert runner._recover(store, run_id)
    else:
        finish_drained(store, run_id, worker_succeeded=False, preview_finished=False)
    if fault == "test_changed":
        (root / "test_fixture.py").write_text("def test_one(): assert False\n")
    if fault == "base_changed":
        source = Path(store.get_task(store.get_run(run_id).task_id).project)
        subprocess.check_call(["git", "-c", "user.name=Fixture","-c","user.email=a@example.org",
                               "commit","--allow-empty","-qm","advanced"], cwd=source)
    if fault == "in_flight":
        recovery.invalidate_checkpoint(db, run_id)
    if fault == "direction":
        db.execute("INSERT INTO studio_directions VALUES(?,?,'developer',1,'inspect',CURRENT_TIMESTAMP,'sending')",
                   (run_id, uuid4().hex))
    if fault == "stop":
        db.execute("UPDATE studio_runs SET stop_requested=1 WHERE id=?", (run_id,))
    monkeypatch.setattr(runner, "_launch", lambda *args: pytest.fail("uncertain work relaunched"))
    if fault == "worker_alive":
        with RunGuard.worker(store.path, run_id):
            result = recovery.reconcile(runner, run_id, uuid4().hex)
            assert result["worker_alive"] and result["disposition"] == "working"
    elif fault == "host_mismatch":
        db.execute("UPDATE runs SET host_name='different-host' WHERE run_id=?", (run_id,))
        with pytest.raises(OfficeError, match="HOST_MISMATCH"):
            recovery.reconcile(runner, run_id, uuid4().hex)
    else:
        assert recovery.reconcile(runner, run_id, uuid4().hex)["disposition"] == ("closed" if fault=="stop" else "inspect")


def test_wait_clock_counts_only_blocked_work_and_never_double_counts_requests(goal):
    _, store, run_id, _ = goal
    db = store.connection
    def stamp(second):
        return f"2026-11-20T00:00:{second:02d}+00:00"
    db.execute("UPDATE runs SET started_at=? WHERE run_id=?", (stamp(0), run_id))
    for holder, end in (("one", None), ("two", stamp(8))):
        db.execute("INSERT INTO studio_goal_workers VALUES(?,?,?,?,?)", (holder, run_id, holder, stamp(2), end))
    for holder, kind, start, end in (("one", "permission", 3, None), ("two", "question", 5, 7)):
        ident = uuid4().hex
        db.execute("""INSERT INTO studio_requests(id,run_id,role,engine,attempt,holder,session_hash,kind,prompt,
            status,created_at,decided_at) VALUES(?,?,'developer','codex',1,?,'session',?,'?',?,?,?)""",
                   (ident, run_id, holder, kind, "decided" if end else "pending", stamp(start), stamp(end) if end else None))
        db.execute("INSERT INTO studio_goal_waits VALUES(?,?)", (ident, stamp(start)))
    assert recovery.timing(db, run_id, at=stamp(12)) == {
        "active_seconds":6, "user_wait_seconds":6, "waiting_for_user":True}

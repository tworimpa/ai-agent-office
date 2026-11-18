"""Studio ownership, exactly-once intent and confirmed descendant termination."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

import pytest

from agent_office.models import OfficeError
from agent_office.ownership import RunBusyError, RunGuard
from agent_office.store import TaskStore
from agent_office.studio_runner import LocalRunner


def wait_for(predicate):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    pytest.fail("Timed out waiting for fixture")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "fixture"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-c", "user.name=studio", "-c", "user.email=fixture@example.invalid",
                    "commit", "--allow-empty", "-qm", "init"], cwd=path, check=True)
    return path


def install_fake(tmp_path, monkeypatch, program):
    script = tmp_path / "codex-fixture.py"
    script.write_text(program, encoding="utf-8")
    if os.name == "nt":
        (tmp_path / "codex.cmd").write_text(f'@"{sys.executable}" "{script}"\n')
    else:
        wrapper = tmp_path / "codex"
        wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))}\n")
        wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])


def process_alive(pid, handles):
    if os.name == "nt":
        import win32api
        import win32event

        # Hold the original handle across cancellation. A PID alone can already
        # identify a new preview/helper process by the next status poll.
        process = win32api.OpenProcess(0x00100000, False, pid)
        handles.callback(process.Close)
        return lambda: win32event.WaitForSingleObject(process, 0) == win32event.WAIT_TIMEOUT

    def exists():
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    return exists


@pytest.mark.parametrize("end", ["stop", "shutdown"])
def test_cancel_tree_concurrent_start_and_reopen(tmp_path, repo, monkeypatch, end):
    db = tmp_path / "tasks.db"
    pidfile = tmp_path / "fixture-pids"
    marker = tmp_path / "calls"
    # Linux fixture explicitly detaches into a new session. Both OSes spawn a
    # grandchild which would keep executing if only the CLI's PID were killed.
    install_fake(tmp_path, monkeypatch,
        "import subprocess, sys, pathlib, os, time\n"
        "sys.stdin.read()\n"
        f"with open({str(marker)!r}, 'a') as f: f.write('x')\n"
        "p = subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'], "
        "start_new_session=os.name != 'nt')\n"
        "pathlib.Path('partial.txt').write_text('local edit')\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(f'{{os.getpid()}} {{p.pid}}')\n"
        "time.sleep(90)\n")
    rid, wid = uuid.uuid4().hex, uuid.uuid4().hex
    args = (rid, wid, repo, ["."], "check original", True)
    with closing(LocalRunner(db)) as runner, ExitStack() as handles:
        with pytest.raises(RunBusyError):
            LocalRunner(db)
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(lambda _: runner.start(*args)["id"], range(4))) == [rid] * 4
        wait_for(lambda: pidfile.is_file() and len(pidfile.read_text().split()) == 2)
        pids = [int(value) for value in pidfile.read_text().split()]
        checks = [process_alive(pid, handles) for pid in pids]
        assert all(check() for check in checks)
        if end == "stop":
            assert runner.stop(rid)["state"] in ("STOP_REQUESTED", "CANCELED")
            wait_for(lambda: runner.detail(rid)["state"] == "CANCELED")
        else:
            runner.close()
        assert not any(check() for check in checks)
    with closing(LocalRunner(db)) as reopened:
        detail = reopened.start(*args)
        assert detail["state"] == detail["outcome"] == "CANCELED"
        assert "partial.txt" in detail["changes"] and not (repo / "partial.txt").exists()
        assert reopened.stop(rid)["state"] == "CANCELED"
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            reopened.start(rid, wid, repo, ["."], "other", True)
    assert marker.read_text() == "x"


def test_plan_response_keeps_full_input_in_memory_and_redacts_persistence(tmp_path, repo, monkeypatch):
    install_fake(tmp_path, monkeypatch,
        "import sys\nprompt=sys.stdin.read()\nassert 'TOKEN=abc123' in prompt\nprint('TOKEN=abc123 ' + 'a'*10000)\n")
    rid = uuid.uuid4().hex
    db = tmp_path / "db"
    with closing(LocalRunner(db)) as runner:
        runner.start(rid, uuid.uuid4().hex, repo, ["."], "review TOKEN=abc123", False)
        wait_for(lambda: runner.detail(rid)["state"] == "PLAN_ONLY")
        detail = runner.stop(rid)
        assert detail["state"] == "PLAN_ONLY"
        assert detail["task"] == "review TOKEN=[redacted]"
        assert detail["developer_output"].startswith("TOKEN=[redacted]")
        assert len(detail["developer_output"].encode()) <= 8192
        assert detail["review_output"] == detail["test_output"] == ""
    assert b"abc123" not in db.read_bytes()


def test_dead_studio_run_requires_recovery_and_never_replays(tmp_path, repo, monkeypatch):
    db = tmp_path / "db"
    rid, wid = uuid.uuid4().hex, uuid.uuid4().hex
    class LaunchFailure(Exception):
        pass

    def crash(*args, **kwargs):
        raise LaunchFailure

    with closing(LocalRunner(db)) as runner, monkeypatch.context() as context:
        context.setattr("agent_office.studio_runner.subprocess.Popen", crash)
        with pytest.raises(LaunchFailure):
            runner.start(rid, wid, repo, ["."], "original", False)
    # Simulate a prior supervisor still draining during the new server's startup.
    with ExitStack() as stack:
        prior = stack.enter_context(RunGuard(db, rid))
        runner = stack.enter_context(closing(LocalRunner(db)))
        assert runner.detail(rid)["state"] == "QUEUED"
        prior.__exit__(None, None, None)
        assert runner.start(rid, wid, repo, ["."], "original", False)["state"] == "RECOVERY_REQUIRED"
        with TaskStore(db, readonly=True) as store:
            run = store.get_run(rid)
            assert run.attempts == 0
            assert [event.event_type for event in store.list_events(run.task_id)] == ["RUN_CREATED", "RECOVERY_REQUIRED"]


def test_final_state_published_after_detached_worker_has_exited(tmp_path, repo, monkeypatch):
    pidfile = tmp_path / "pid"
    proceed = tmp_path / "continue"
    install_fake(tmp_path, monkeypatch,
        "import pathlib, os, sys, time, subprocess\n"
        "sys.stdin.read()\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(90)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt')\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n"
        f"while not pathlib.Path({str(proceed)!r}).exists(): time.sleep(0.01)\n"
        "print('completed')\n")
    db = tmp_path / "db"
    rid = uuid.uuid4().hex
    with closing(LocalRunner(db)) as runner, ExitStack() as handles:
        runner.start(rid, uuid.uuid4().hex, repo, ["."], "plan", False)
        wait_for(lambda: pidfile.exists() and pidfile.read_text().isdigit())
        check = process_alive(int(pidfile.read_text()), handles)
        assert check()
        proceed.touch()
        wait_for(lambda: runner.detail(rid)["state"] == "PLAN_ONLY")
        assert not check()
        assert runner.stop(rid)["state"] == "PLAN_ONLY"


def test_retry_does_not_publish_previous_review_or_test(tmp_path):
    from agent_office.adapters import AgentResult
    from agent_office.workflow import build_graph
    from test_workflow import MockAdapter, base_state

    developer = MockAdapter([AgentResult(0, "first"), AgentResult(127, "second attempt failed")])
    reviewer = MockAdapter([AgentResult(0, "VERDICT: CHANGES_REQUESTED\nMissing feature")])
    outputs = {}
    graph = build_graph({"claude": developer, "codex": reviewer}, save_stage=outputs.__setitem__)
    result = graph.invoke(base_state(tmp_path))
    assert result["status"] == "FAILED" and "Missing feature" in developer.calls[1][0]
    assert outputs == {"developer_output": "second attempt failed", "test_output": "", "review_output": ""}
    assert result["test_output"] == result["review_output"] == ""

@pytest.mark.parametrize(("stop_first", "worker_succeeded", "expected"), [
    (True, False, "CANCELED"), (False, True, "PLAN_ONLY"), (False, False, "RECOVERY_REQUIRED")
])
def test_stop_serializes_with_pending_completion(tmp_path, repo, monkeypatch, stop_first, worker_succeeded, expected):
    from io import BytesIO
    from types import SimpleNamespace
    from agent_office.studio_runner import finish_drained

    db, rid = tmp_path / "db", uuid.uuid4().hex
    child = SimpleNamespace(stdin=BytesIO(), poll=lambda: None, wait=lambda timeout: 0)
    with closing(LocalRunner(db)) as runner, TaskStore(db) as store:
        monkeypatch.setattr("agent_office.studio_runner.subprocess.Popen", lambda *args, **kwargs: child)
        runner.start(rid, uuid.uuid4().hex, repo, ["."], "plan", False)
        store.record_transition(rid, operation_key=f"{rid}:dev", event_type="STAGE_STARTED", state="RUNNING")
        store.connection.execute("UPDATE studio_runs SET proposed_state='PLAN_ONLY',proposed_outcome='PLAN_ONLY' WHERE id=?", (rid,))
        if stop_first:
            assert runner.stop(rid)["state"] == "STOP_REQUESTED"
        finish_drained(store, rid, worker_succeeded=worker_succeeded, preview_finished=False)
        assert runner.stop(rid)["state"] == expected
        terminal = [e for e in store.list_events(store.get_run(rid).task_id)
                    if e.event_type in {"RUN_FINISHED", "RECOVERY_REQUIRED"}]
        assert len(terminal) == 1 and terminal[0].payload["state"] == expected

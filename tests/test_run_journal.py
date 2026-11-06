import json
import sqlite3
import subprocess
import sys
import os
import signal
import time
from pathlib import Path
import pytest
from agent_office import cli
from agent_office.adapters import AgentResult
from agent_office.events import RunRecorder
from agent_office.store import TaskStore
from agent_office.workflow import build_graph
from test_store import seeded, TASK, RUN
from test_workflow import MockAdapter, base_state


@pytest.mark.parametrize("edits,expected,count", [(False, "PLAN_ONLY", 5), (True, "READY_FOR_HUMAN_REVIEW", 9)])
def test_run_is_durable_with_exact_stage_sequence(tmp_path, monkeypatch, capsys, edits, expected, count):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "init"], cwd=repo, check=True)
    calls = []
    class Fake:
        def __init__(self, engine, **kwargs):
            self.engine = engine
        def execute(self, prompt, **kwargs):
            calls.append(self.engine)
            return AgentResult(0, "VERDICT: APPROVED" if self.engine == "codex" else "original response")
    monkeypatch.setattr(cli, "CLIAdapter", Fake)
    path = tmp_path / "db"
    args = ["--db", str(path), "run", "--repo", str(repo), "--task", "한글 task", "--test-command", sys.executable, "-c", "pass"]
    # nargs='+' does not accept '-c'; use a real script instead.
    test_file = tmp_path / "check.py"
    test_file.write_text("pass\n")
    args = args[:-3] + [sys.executable, str(test_file)]
    if edits:
        args += ["--allow-edits"]
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    with TaskStore(path, readonly=True) as store:
        assert store.get_task(report["task_id"]).state == expected
        events = store.list_events(report["task_id"])
        assert len(events) == count
        assert [e.seq for e in events] == list(range(1, count+1))
        assert events[-1].event_type == "RUN_FINISHED"
        assert "original response" not in str(events)
    assert calls == (["claude", "codex"] if edits else ["claude"])
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo) == b""


@pytest.mark.parametrize("phase", ["developer", "test", "review"])
def test_timeout_never_retries_even_with_budget(tmp_path, monkeypatch, phase):
    with seeded(tmp_path / "db") as store:
        developer = MockAdapter([AgentResult(124 if phase == "developer" else 0, "done")])
        reviewer = MockAdapter([AgentResult(124, "timeout")])
        if phase == "test":
            def timeout(*args, **kwargs):
                raise subprocess.TimeoutExpired("test", 300)
            monkeypatch.setattr("agent_office.workflow.subprocess.run", timeout)
        graph = build_graph({"claude": developer, "codex": reviewer}, recorder=RunRecorder(store, RUN))
        result = graph.invoke(base_state(tmp_path))
        assert result["status"] == "RECOVERY_REQUIRED"
        assert len(developer.calls) == 1
        assert len(reviewer.calls) == (1 if phase == "review" else 0)


@pytest.mark.parametrize("fail_at,expected_calls", [("STAGE_STARTED", 0), ("STAGE_COMPLETED", 1)])
def test_journal_failure_stops_external_execution(tmp_path, fail_at, expected_calls):
    with seeded(tmp_path / "db") as store:
        store.connection.execute(f"CREATE TRIGGER fail BEFORE INSERT ON events WHEN NEW.event_type='{fail_at}' BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        developer = MockAdapter([AgentResult(0, "done")])
        reviewer = MockAdapter([])
        graph = build_graph({"claude": developer, "codex": reviewer}, recorder=RunRecorder(store, RUN))
        with pytest.raises(sqlite3.IntegrityError):
            graph.invoke(base_state(tmp_path))
        assert len(developer.calls) == expected_calls
        assert not reviewer.calls


@pytest.mark.parametrize("window,worker_calls", [("intent", 0), ("workspace", 0), ("started", 1), ("completed", 1)])
def test_coordinator_crash_never_replays_worker(tmp_path, window, worker_calls):
    import platform
    from agent_office.ownership import recover_run
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "init"], cwd=repo, check=True)
    db, marker, counter, pidfile = [tmp_path / name for name in ("db", "ready", "calls", "pid")]
    helper = Path(__file__).parent / "helpers" / "crash_driver.py"
    process = subprocess.Popen([sys.executable, str(helper), str(db), str(repo), window, str(marker), str(counter), str(pidfile)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), process.poll()
        process.kill()
        process.wait(timeout=5)
        before = counter.read_text() if counter.exists() else ""
        assert before.count("call") == worker_calls
        with TaskStore(db) as store:
            task = store.list_tasks()[0]
            assert task.state in {"QUEUED", "RUNNING"}
            recover_run(store, task.current_run_id, host_name=platform.node())
            assert store.get_task(task.task_id).state == "RECOVERY_REQUIRED"
            count = len(store.list_events(task.task_id))
            recover_run(store, task.current_run_id, host_name=platform.node())
            assert len(store.list_events(task.task_id)) == count
        assert (counter.read_text() if counter.exists() else "") == before
    finally:
        if process.poll() is None:
            process.kill()
        if window == "started" and pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.communicate(timeout=5)

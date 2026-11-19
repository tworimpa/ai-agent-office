import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from agent_office import studio_improvements as improvements
from agent_office.engines import resolve_executable
from agent_office.models import OfficeError, OwnerInfo, TaskSpec
from agent_office.store import TaskStore
from agent_office.workspace import Worktree
from test_improvement_proposal import fixture, OTHER, PATCH, RUN


JOB = "a" * 32


def wait_until(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    pytest.fail("The improvement worker did not reach the expected state")


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    folder = tmp_path / "mock cli"
    folder.mkdir()
    script = folder / "fake.py"
    script.write_text("""
import json
from pathlib import Path
import sys
import time
folder = Path(__file__).parent
history = json.loads(sys.stdin.read().splitlines()[-1])
with (folder / "calls").open("a") as calls:
    print("called", file=calls)
mode = (folder / "mode").read_text()
if mode == "wait":
    while not (folder / "release").is_file():
        time.sleep(0.02)
if mode == "failed":
    sys.exit(1)
response = {} if mode == "none" else {
    "title": "Clarify failure", "rationale": "A selected test run failed.",
    "expected": "Identify the failed test.", "patch": """ + repr(PATCH) + """,
    "citations": [{"run_id": r["run_id"], "event": r["events"][-1]} for r in history]}
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(response)}}))
print(json.dumps({"type": "turn.completed"}))
""", encoding="utf-8")
    if os.name == "nt":
        launcher = folder / "codex.cmd"
        launcher.write_text(f'@"{sys.executable}" "%~dp0fake.py"\n')
    else:
        launcher = folder / "codex"
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))}\n')
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ["PATH"])
    assert Path(resolve_executable("codex")) == launcher
    (folder / "mode").write_text("wait")
    return folder


def test_disconnect_replay_and_receipted_operator_import(fixture, fake_cli):
    db, source, base = fixture
    assert list(improvements.ready_job_ids(db)) == []
    service = improvements.StudioImprovements(db, source)
    try:
        assert {r["id"] for r in service.runs()["items"]} == {RUN, OTHER}
        assert service.jobs() == {"items": [], "next_offset": None}
        assert service.start(JOB, base, "codex", [RUN])["state"] == "generating"
        wait_until((fake_cli / "calls").exists)
        assert service.start(JOB, base, "codex", [RUN]) == service.job(JOB)
        for changed in ((base, "codex", [OTHER]), (base, "gemini", [RUN]), ("e" * 40, "codex", [RUN])):
            with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
                service.start(JOB, *changed)
        with pytest.raises(OfficeError, match="RUN_BUSY"):
            service.start("b" * 32, base, "codex", [RUN])
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            service.artifact(JOB)
        (fake_cli / "release").touch()
        wait_until(lambda: service.job(JOB)["state"] != "generating")
        job = service.job(JOB)
        assert job == dict(id=JOB, state="ready", message="", engine="codex", title="Clarify failure",
                           run_ids=[RUN], base=base)
        artifact = service.artifact(JOB)
        assert artifact["patch"] == PATCH and artifact["history"][0]["run_id"] == RUN
        assert list(improvements.ready_job_ids(db)) == [JOB]
        assert improvements.ready_artifact(db, JOB) == artifact
    finally:
        service.close()
    restarted = improvements.StudioImprovements(db, None)
    try:
        assert restarted.jobs()["items"] == [job]
        assert restarted.start(JOB, base, "codex", [RUN]) == job
        with pytest.raises(ValueError):
            restarted.start("b" * 32, base, "codex", [RUN])
        assert restarted.artifact(JOB) == artifact
        with TaskStore(db) as store:
            store.connection.execute("UPDATE studio_improvements SET run_ids=?", (json.dumps([OTHER]),))
        with pytest.raises(ValueError):
            restarted.artifact(JOB)
        with TaskStore(db) as store:
            store.connection.execute("UPDATE studio_improvements SET run_ids=?,artifact=?",
                                     (json.dumps([RUN]), json.dumps(dict(artifact, expected="Unsupported prediction"))))
        with pytest.raises(ValueError):
            improvements.ready_artifact(db, JOB)
        with TaskStore(db) as store:
            store.connection.execute("UPDATE studio_improvements SET artifact=?", (json.dumps(artifact),))
            store.connection.execute("DELETE FROM improvement_receipts")
        with pytest.raises(ValueError):
            restarted.artifact(JOB)
    finally:
        restarted.close()
    assert (fake_cli / "calls").read_text().splitlines() == ["called"]


@pytest.mark.parametrize("mode,expected", [("none", "no_proposal"), ("failed", "failed")])
def test_distinct_terminal_results(fixture, fake_cli, mode, expected):
    db, source, base = fixture
    (fake_cli / "mode").write_text(mode)
    service = improvements.StudioImprovements(db, source)
    try:
        service.start(JOB, base, "codex", [RUN])
        wait_until(lambda: service.job(JOB)["state"] != "generating")
        assert service.job(JOB)["state"] == expected
        assert service.job(JOB)["message"]
        assert not list(improvements.ready_job_ids(db))
    finally:
        service.close()


def test_shutdown_drains_active_cli_and_restart_never_replays(fixture, fake_cli):
    db, source, base = fixture
    service = improvements.StudioImprovements(db, source)
    try:
        service.start(JOB, base, "codex", [RUN])
        wait_until((fake_cli / "calls").exists)
    finally:
        service.close()
    assert not service.monitor.is_alive()
    (fake_cli / "release").touch()
    # Also simulate a committed submission whose previous owner died before starting its supervisor.
    with TaskStore(db) as store:
        store.connection.execute("""INSERT INTO studio_improvements(id,base,engine,run_ids,state)
                                    VALUES (?,?,?,?,'generating')""", ("b" * 32, base, "codex", json.dumps([RUN])))
    restarted = improvements.StudioImprovements(db, source)
    try:
        assert {row["state"] for row in restarted.jobs()["items"]} == {"interrupted"}
        assert restarted.start(JOB, base, "codex", [RUN])["state"] == "interrupted"
        improvements._finish(db, JOB, "failed", "stale owner")
        assert restarted.job(JOB)["state"] == "interrupted"
        assert list(improvements.ready_job_ids(db)) == []
    finally:
        restarted.close()
    assert (fake_cli / "calls").read_text().splitlines() == ["called"]


def test_terminal_cli_runs_page_and_explicit_selection(fixture, fake_cli):
    db, source, base = fixture
    with TaskStore(db) as store:
        for number in range(26):
            run_id = uuid4().hex
            store.create_run(TaskSpec(uuid4().hex, "demo", "TOKEN=private request", "codex", "claude", 1),
                             run_id, Worktree(source, "work"), OwnerInfo("host", "owner"))
            if number != 25:
                store.record_transition(run_id, operation_key="done", event_type="RUN_FINISHED",
                                        state="FAILED", outcome="TEST_FAILED")
    service = improvements.StudioImprovements(db, source)
    try:
        first = service.runs(0)
        assert first["next_offset"] == 24 and len(first["items"]) == 24
        last = service.runs(24)
        assert last["next_offset"] is None and len(last["items"]) == 3
        entries = first["items"] + last["items"]
        assert len({row["id"] for row in entries}) == 27 and run_id not in {row["id"] for row in entries}
        assert all("private" not in row["request"] and "hidden" not in row["request"] for row in entries)
        assert all(len(row["request"].encode()) <= 1024 for row in entries)
        for selection in ([], [RUN] * 9, [RUN, RUN], [run_id], ["no"], "bad"):
            with pytest.raises(ValueError):
                service.start(JOB, base, "codex", selection)
        for offset in (True, -1, 1, 2**60):
            with pytest.raises(ValueError):
                service.runs(offset)
            with pytest.raises(ValueError):
                service.jobs(offset)
        assert service.jobs()["items"] == [] and not (fake_cli / "calls").exists()
    finally:
        service.close()

def test_owner_process_crash_during_generation(fixture, fake_cli):
    db, source, base = fixture
    owner_code = """
import os, sys
from pathlib import Path
from agent_office.studio_improvements import StudioImprovements
db, repo, job, base, run = sys.argv[1:]
owner = StudioImprovements(Path(db), Path(repo))
owner.start(job, base, "codex", [run])
sys.stdin.buffer.read(1)
os._exit(41)
"""
    with subprocess.Popen([sys.executable, "-c", owner_code, str(db), str(source), JOB, base, RUN],
                          cwd=Path(improvements.__file__).resolve().parents[1], stdin=subprocess.PIPE) as owner:
        try:
            wait_until((fake_cli / "calls").exists)
        finally:
            owner.communicate(b"x", timeout=15)
        assert owner.returncode == 41
    service = improvements.StudioImprovements(db, source)
    try:
        assert service.start(JOB, base, "codex", [RUN])["state"] == "interrupted"
        wait_until(lambda: not improvements._slot(db).is_held())
        assert service.job(JOB)["state"] == "interrupted"
        (fake_cli / "release").touch()
        assert service.start("b" * 32, base, "codex", [RUN])["state"] == "generating"
        wait_until(lambda: service.job("b" * 32)["state"] == "ready")
    finally:
        service.close()
    assert (fake_cli / "calls").read_text().splitlines() == ["called", "called"]

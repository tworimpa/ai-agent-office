import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from agent_office import goal_checks
from agent_office.store import TaskStore
from test_studio_http import request, studio
from test_workspace_rooms import engine_snapshot


@pytest.mark.parametrize("fault", ["checkpoint", "external"])
def test_restart_preserves_real_goal_and_never_repeats_external_work(tmp_path, monkeypatch, fault):
    if not goal_checks._unprivileged():
        pytest.skip("requires an ordinary execution host for the real pytest subprocess")
    database, root, mock_bin = tmp_path / "studio.db", tmp_path / "fixture", tmp_path / "fixture-bin"
    root.mkdir()
    mock_bin.mkdir()
    marker = tmp_path / "candidate-test-started"
    source = Path(__file__).resolve().parents[1] / "desktop/fixtures/goal_worker.py"
    harness = Path(__file__).with_name("fixtures") / "goal_crash_process.py"
    for alias in ("codex", "agy"):
        if os.name == "nt":
            (mock_bin / (alias + ".cmd")).write_text(f'@"{sys.executable}" "{source}" {alias} %*\r\n')
        else:
            command = mock_bin / alias
            command.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(source))} {alias} "$@"\n')
            command.chmod(0o755)
    monkeypatch.setenv("PATH", str(mock_bin) + os.pathsep + os.environ["PATH"])
    (root / "settings.py").write_bytes(b'import json\n\ndef restore(path):\n    return "light"\n')
    code = ('from pathlib import Path\nfrom settings import restore\nimport time\n\n'
            'def test_saved(tmp_path):\n    path = tmp_path / "settings.json"\n'
            '    path.write_text(\'{"theme":"dark"}\')\n    assert restore(path) == "dark"\n')
    if fault == "external":
        code += f'    Path({str(marker)!r}).write_text("running")\n    time.sleep(90)\n'
    (root / "test_saved.py").write_text(code, encoding="utf8")
    for args in (("init","-q"), ("add","."), ("-c","user.name=Fixture","-c","user.email=a@example.invalid",
                                                   "commit","-qm","original bug")):
        subprocess.check_call(["git",*args], cwd=root)
    run_id = uuid4().hex
    route = "/api/runs/"+run_id+"/goal"
    def finished():
        deadline = time.monotonic()+90
        with TaskStore(database) as store:
            while time.monotonic()<deadline:
                run = store.get_run(run_id)
                if run.ended_at:
                    return run.state
                time.sleep(.1)
        raise AssertionError("fixture worker did not exit")
    with studio(database) as server:
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("codex","antigravity"))
        def launch(store, identifier, prompt):
            command = [sys.executable,str(harness),"supervise",fault,str(marker),str(store.path),identifier]
            worker = subprocess.Popen(command, stdin=subprocess.PIPE)
            server.runner.children[identifier] = worker
            worker.stdin.write(json.dumps(prompt).encode()+b"\n")
            worker.stdin.flush()
        monkeypatch.setattr(server.runner,"_launch",launch)
        _, body = request(server,"/api/workspaces",{"name":"settings","paths":[str(root)]})
        room = "/api/rooms/"+body["workspace"]["id"]
        payload = {"id":run_id, "message":"재현하고 저장 설정 수정. 기본 테마는 밝게.", "goal":True, "allow_edits":True,
                   "delegation":False, "goal_criteria":["저장하면 테마가 유지된다."], "test_files":["test_saved.py"]}
        status, receipt = request(server,room+"/assignments",payload)
        assert status==202, receipt
        assert finished()=="RECOVERY_REQUIRED", request(server,route)
    with studio(database) as server:
        report = request(server,route)[1]["goal"]
        assert [(c["attempt"],c["exit_code"]) for c in report["checks"][:2]] == [(0,1),(0,1)]
        assert report["id"]==run_id and report["base_commit"]
        key = {"id":uuid4().hex}
        expected = "resume_available" if fault=="checkpoint" else "inspect"
        assert report["recovery"]["disposition"]==expected, report["recovery"]
        first = request(server,route+"/recover",key)[1]["recovery"]["receipt"]
        again = request(server,route+"/recover",key)[1]["recovery"]["receipt"]
        assert first==again
        if fault=="external":
            assert marker.read_text()=="running" and first["disposition"]=="inspect"
            assert report["checks"][-1]["status"]=="running"
            assert finished()=="RECOVERY_REQUIRED"
        else:
            assert first["disposition"]=="resumed"
            assert finished()=="READY_FOR_HUMAN_REVIEW", request(server,route)
            final = request(server,route)[1]["goal"]
            assert [(c["attempt"],c["exit_code"]) for c in final["checks"]]==[(0,1),(0,1),(1,0),(1,0)]
            assert len(final["candidates"])==1 and final["candidates"][0]["review_status"]=="approved"
        with TaskStore(database) as store:
            dev = store.connection.execute("SELECT count(*) FROM events WHERE run_id=? AND role='developer' "
                                           "AND event_type='STAGE_STARTED'",(run_id,)).fetchone()
            assert dev[0]==1
            changed = Path(store.get_task(store.get_run(run_id).task_id).workspace)/"settings.py"
            assert b"json.loads" in changed.read_bytes()

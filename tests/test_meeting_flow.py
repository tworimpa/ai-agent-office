"""Public room assignment, approval and execution through fixture CLI subprocesses."""
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import sys
import time
from uuid import uuid4

import pytest

from agent_office.studio_ssh import RemoteHost, RemoteUnavailable, _read_response
from test_remote_studio import LoopbackLink
from test_studio_http import project, request, studio, wait_for
from test_studio_ssh import frame
from test_workspace_rooms import engine_snapshot, register


MOCK = r'''
import json, os, sys
from pathlib import Path
engine = sys.argv[1]
if "--help" in sys.argv or "--version" in sys.argv:
    print("fixture")
    raise SystemExit(0)
prompt = sys.stdin.buffer.read().decode("utf-8")
if engine == "agy":
    prompt = json.loads(prompt)["message"]["content"]
stage = prompt.splitlines()[0]
if trace_path := os.environ.get("OFFICE_TEST_BOT_PROMPTS"):
    with open(trace_path, "a", encoding="utf-8") as log:
        log.write(json.dumps([engine, prompt]) + "\n")
if stage in ("MEETING_DEVELOPER", "MEETING_REVIEWER", "MEETING_COORDINATOR"):
    material = json.loads(prompt.splitlines()[-1])
    assert "web/test_value.py" in material["evidence"]["excerpts"]
    if stage == "MEETING_REVIEWER":
        assert material["previous"]["opinion"] == "개발 의견"
    plan = {"outcome": "설정 값 변경", "scope": ["web"], "rationale": ["기존 test_value 사용"],
            "tasks": [{"id":"A","owner":"developer","description":"값 변경","depends_on":[]},
                      {"id":"B","owner":"reviewer","description":"값 검토","depends_on":["A"]}],
            "criteria": [{"criterion":"값 일치","verification":"지정 테스트","test_files":["web/test_value.py"]},
                         {"criterion":"화면 표시","verification":"사용자 확인"}],
            "questions": [] if "PRODUCT" not in material["request"] else
                [{"question":"어느 화면?", "options":["장바구니","홈"]},
                 {"question":"첫 표시?", "options":["확장","접힘"]}]}
    output = json.dumps({"opinion":"개발 의견" if engine == "claude" else "검토 의견",
                         "issues":[], "plan":plan}, ensure_ascii=False)
elif engine == "claude":
    stage = "edit"
    assert "설정 값 변경" in prompt
    if "PRODUCT 설정" in prompt:
        assert "편집한 목표" in prompt and "장바구니" in prompt and "접힘" in prompt
    Path("web/value.txt").write_text("new")
    output = "값 변경 완료"
else:
    stage = "review"
    assert "1 passed" in prompt
    assert Path("web/value.txt").read_text() == "new"
    output = "VERDICT: APPROVED"
with open(os.environ["OFFICE_MEETING_CALLS"], "a", encoding="utf-8") as trace:
    trace.write(engine + " " + stage + "\n")
reply = ({"type":"result","subtype":"success","is_error":False,"result":output} if engine == "claude"
         else {"status":"SUCCESS","response":output})
print(json.dumps(reply))
'''


@pytest.fixture
def setup(tmp_path, monkeypatch):
    tools = tmp_path / "meeting tools"
    tools.mkdir()
    script = tools / "fixture.py"
    script.write_text(MOCK, encoding="utf-8")
    for engine in ("claude", "agy"):
        if os.name == "nt":
            (tools / (engine + ".cmd")).write_text(f'@"{sys.executable}" "%~dp0fixture.py" {engine} %*\n')
        else:
            wrapper = tools / engine
            wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {engine} "$@"\n')
            wrapper.chmod(0o700)
    trace = tmp_path / "calls"
    monkeypatch.setenv("OFFICE_MEETING_CALLS", str(trace))
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    return project(tmp_path), trace


def assign(server, folders, monkeypatch, task, *, api="/api", peer=None):
    monkeypatch.setattr((peer or server).engines, "snapshot", lambda: engine_snapshot("claude", "antigravity"))
    workspace_id = register(server, folders, api=api)
    room = f"{api}/rooms/{workspace_id}"
    settings = request(server, room)[1]["settings"]
    key = uuid4().hex
    status, body = request(server, room + "/assignments", {
        "id": key, "meeting": True, "review_plan": False, "allow_edits": True,
        "message": task, "delegation": False, "test_files": ["web/test_value.py"], "role_engines": {"developer": "claude"},
    })
    assert status == 202, body
    assert body["run"]["developer_engine"] == "claude"
    assert request(server, room)[1]["settings"] == settings
    return key


def await_gate(server, path):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status, data = request(server, path)
        assert status == 200, data
        run = data["run"]
        assert run["state"] not in ("FAILED", "RECOVERY_REQUIRED"), run
        if run["meeting"]["status"] == "awaiting_review":
            return run["meeting"]
        time.sleep(.1)
    pytest.fail("The product decision gate did not open")


def test_room_auto_executes_verifies_and_preserves_meeting_journal(tmp_path, monkeypatch, setup):
    folders, trace = setup
    db = tmp_path / "studio.db"
    with studio(db) as server:
        key = assign(server, folders, monkeypatch, "설정 값 변경")
        run = wait_for(server, key, {"READY_FOR_HUMAN_REVIEW"})
        meeting = run["meeting"]
        assert meeting["status"] == "approved" and meeting["decision"] is None
        assert meeting["revisions"][0]["approved_by"] == "coordinator"
        assert [(item["role"], item["engine"], item["text"]) for item in meeting["contributions"]] == [
            ("developer", "claude", "개발 의견"), ("reviewer", "antigravity", "검토 의견")]
        assert [item["status"] for item in meeting["verification"]] == ["passed", "not_verified"]
        assert "1 passed" in run["test_output"] and "APPROVED" in run["review_output"]
        invocation = json.loads(run["test_output"].splitlines()[1])
        assert invocation["cwd"] == run["workspace"] and invocation["exit_code"] == 0
        assert invocation["argv"][-1] == "web/test_value.py"
        assert "web/value.txt" in run["changes"] and folders[0].joinpath("value.txt").read_text() == "old"
        assert trace.read_text().splitlines() == ["claude MEETING_DEVELOPER", "agy MEETING_REVIEWER", "claude edit", "agy review"]
    with studio(db) as reopened:
        assert request(reopened, f"/api/runs/{key}")[1]["run"]["meeting"] == meeting


def test_workspace_role_bots_pin_handoffs_and_each_workers_own_instructions(tmp_path, monkeypatch, setup):
    from agent_office.cli import main
    from agent_office.studio_server import StudioServer

    folders, _ = setup
    db = tmp_path / "bots.db"
    log = tmp_path / "prompts.jsonl"
    monkeypatch.setenv("OFFICE_TEST_BOT_PROMPTS", str(log))
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path))
    with StudioServer(db, port=0) as server:
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("claude", "antigravity"))
        workspace = server.catalog.create("My office", [str(path) for path in folders])["id"]
        roles = ("coordinator", "developer", "reviewer")
        for role in roles:
            args = ["--workspace-id", workspace, "--id", f"custom-{role}", "--role", role]
            assert main(["--db", str(db), "bots", "create", *args, "--name", f"My {role}",
                         "--instructions", f"role-message-{role}"]) == 0
            assert main(["--db", str(db), "bots", "assign", *args]) == 0
        run_id = uuid4().hex
        body = {"id": run_id, "meeting": True, "review_plan": True, "allow_edits": True,
                "message": "설정 값 변경", "delegation": False, "test_files": ["web/test_value.py"],
                "role_engines": {"developer": "claude"}}
        receipt = server.rooms.assign(workspace, body)["assignment"]
        assert {role: bot["id"] for role, bot in receipt["bots"].items()} == {r: f"custom-{r}" for r in roles}
        assert "instructions" not in json.dumps(receipt["bots"])
        assert receipt["roles"]["developer"] == {"name": "My developer", "engine": "claude", "skills": []}
        def wait_until(predicate):
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                run = server.runner.detail(run_id)
                if predicate(run):
                    return run
                assert run["state"] not in ("FAILED", "RECOVERY_REQUIRED", "WAITING_USER"), run
                time.sleep(0.1)
            pytest.fail("Timed out waiting for the role bot workflow")

        meeting = wait_until(lambda run: run["meeting"]["status"] == "awaiting_review")["meeting"]
        assert meeting["coordinator_plan"] and meeting["revisions"][0]["approved_by"] is None
        assert [item["role"] for item in meeting["contributions"]] == ["coordinator"]
        waiting = server.runner.detail(run_id)
        assert not waiting["developer_output"] and not waiting["review_output"]
        assert (Path(waiting["workspace"]) / "web/value.txt").read_text() == "old"
        calls_before_approval = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert len(calls_before_approval) == 1
        assert calls_before_approval[0][1].startswith("MEETING_COORDINATOR\n")
        for role in roles:
            assert main(["--db", str(db), "bots", "assign", "--workspace-id", workspace,
                         "--role", role, "--id", f"office-{role}"]) == 0
        assert server.rooms.assign(workspace, body)["assignment"] == receipt
        decision = {"id": uuid4().hex, "revision": meeting["revision"], "plan": meeting["plan"], "answers": {}}
        server.runner.decide_meeting(run_id, decision)
        final = wait_until(lambda run: run["state"] == "READY_FOR_HUMAN_REVIEW")
        assert final["assignment"] == receipt
        assert final["meeting"]["verification"][0]["status"] == "passed"
        assert final["meeting"]["revisions"][0]["approved_by"] == "operator"
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert [engine for engine, _ in calls] == ["claude", "claude", "agy"]
        for index, (engine, prompt) in enumerate(calls):
            assert "role-message-coordinator" in prompt
            assert ("role-message-developer" in prompt) == (index == 1)
            assert ("role-message-reviewer" in prompt) == (engine == "agy")
            if index:
                assert "설정 값 변경" in prompt and '"depends_on": ["A"]' in prompt
    with StudioServer(db, port=0) as reopened:
        assert reopened.runner.detail(run_id)["assignment"] == receipt


def test_remote_product_gate_applies_one_edited_plan_and_authenticates_receipt(tmp_path, monkeypatch, setup):
    folders, trace = setup
    with studio(tmp_path / "peer.db", accept_remote=True) as peer:
        host = RemoteHost("peer", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)

        def forward(target, method, route, payload):
            status, body = link(target, method, route, payload)
            return _read_response(BytesIO(frame(json.dumps(body).encode(), status, host.journal_id)),
                                  host.journal_id, method, route, payload)

        monkeypatch.setattr("agent_office.studio_server.remote_request", forward)
        api = "/api/hosts/" + host.journal_id
        with studio(tmp_path / "local.db", remotes=(host,)) as server:
            key = assign(server, folders, monkeypatch, "PRODUCT 설정 값 변경", api=api, peer=peer)
            route = f"/runs/{key}/meeting/decision"
            meeting = await_gate(server, f"{api}/runs/{key}")
            assert trace.read_text().splitlines() == ["claude MEETING_DEVELOPER", "agy MEETING_REVIEWER"]
            assert len(meeting["choices"]) == 2
            decision = {"id": uuid4().hex, "revision": meeting["revision"], "plan": deepcopy(meeting["plan"]),
                        "answers": {"q1": "장바구니", "q2": "접힘"}}
            decision["plan"]["outcome"] = "편집한 목표 - 설정 값 변경"
            assert request(server, api + route, {**decision, "answers": {"q1": "홈"}})[0] == 400
            status, receipt = request(server, api + route, decision)
            assert status == 200, receipt
            approved = receipt["meeting"]
            assert approved["revision"] == 2 and approved["revisions"][-1]["source"] == "operator"
            assert approved["answers"] == decision["answers"] and approved["plan"] == decision["plan"]
            completed = wait_for(server, key, {"READY_FOR_HUMAN_REVIEW"}, api=api)
            assert completed["meeting"]["verification"][0]["status"] == "passed"
            assert request(server, f"/api/runs/{key}")[0] == 404
            assert request(server, api + route, decision)[0] == 200
            # A correctly framed, internally consistent response still has to
            # acknowledge this journal and the exact approved operator decision.
            for fault in ("journal", "id", "plan", "answers"):
                forged = deepcopy(receipt)
                record = forged["meeting"]
                if fault == "id":
                    record["decision"]["id"] = uuid4().hex
                elif fault == "plan":
                    record["plan"]["outcome"] = record["revisions"][-1]["plan"]["outcome"] = "다른 목표"
                elif fault == "answers":
                    record["answers"]["q1"] = record["revisions"][-1]["answers"]["q1"] = "홈"
                header = uuid4().hex if fault == "journal" else host.journal_id
                with pytest.raises(RemoteUnavailable):
                    _read_response(BytesIO(frame(json.dumps(forged).encode(), 200, header)),
                                   host.journal_id, "POST", route, decision)


def test_room_stop_while_gate_open_prevents_execution_and_late_approval(tmp_path, monkeypatch, setup):
    folders, trace = setup
    with studio(tmp_path / "stop.db") as server:
        key = assign(server, folders, monkeypatch, "PRODUCT 설정 값 변경")
        path = f"/api/runs/{key}"
        gate = await_gate(server, path)
        assert request(server, path + "/stop", {})[0] == 200
        result = wait_for(server, key, {"CANCELED"})
        assert result["meeting"]["status"] == "stopped"
        late = {"id": uuid4().hex, "revision": gate["revision"], "plan": gate["plan"],
                "answers": {"q1": "홈", "q2": "접힘"}}
        assert request(server, path + "/meeting/decision", late)[0] == 409
        assert trace.read_text().splitlines() == ["claude MEETING_DEVELOPER", "agy MEETING_REVIEWER"]

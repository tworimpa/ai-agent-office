"""A live fixture session pauses twice and continues via the host's public inbox."""
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

import pytest

from agent_office.studio_ssh import RemoteHost, RemoteUnavailable, _read_response, is_peer_route
from test_codex_inbox import install_mock_codex
from test_remote_studio import LoopbackLink
from test_studio_http import project, request, studio, wait_for, workers
from test_studio_ssh import frame


def install_inbox_fixture(tmp_path, monkeypatch):
    workers(tmp_path, monkeypatch)
    name = tmp_path / "mock cli" / "cline"
    script = Path(__file__).parent / "helpers" / "inbox_session.py"
    if os.name == "nt":
        name.with_suffix(".cmd").write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        name.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
        name.chmod(0o700)
    trace = tmp_path / "inbox-trace.txt"
    monkeypatch.setenv("OFFICE_INBOX_TRACE", str(trace))
    return trace


def wait_request(local, api, run, kind):
    timeout = time.monotonic() + 40
    while time.monotonic() < timeout:
        status, data = request(local, api + "/inbox")
        assert status == 200, data
        for row in data["requests"]:
            if row["run_id"] == run and row["kind"] == kind:
                return row
        _, detail = request(local, f"{api}/runs/{run}")
        assert detail["run"]["state"] in ("QUEUED", "RUNNING", "REVIEWING"), detail
        time.sleep(.1)
    pytest.fail("Timed out waiting for " + kind)


def test_remote_decision_receipt_matches_host_run_request_and_action():
    pin, run, request_id = (uuid.uuid4().hex for _ in range(3))
    decision = {"action": "approve"}
    path = f"/runs/{run}/inbox/{request_id}/decision"
    row = {"id": request_id, "run_id": run, "kind": "permission", "status": "decided",
           "role": "developer", "engine": "cline", "prompt": "편집 내용", "decision": "approve"}

    def receipt(record, journal):
        data = {"request": record}
        return _read_response(BytesIO(frame(json.dumps(data).encode(), 200, journal)), pin, "POST", path, decision)

    assert is_peer_route("POST", path)
    assert not is_peer_route("GET", path) and not is_peer_route("POST", "/inbox")
    assert receipt(row, pin) == (200, {"request": row})
    with pytest.raises(RemoteUnavailable):
        receipt(row, run)
    for replacement in ({"id": run}, {"run_id": pin}, {"kind": "question"},
                        {"decision": "reject"}, {"status": "pending"}):
        with pytest.raises(RemoteUnavailable):
            receipt({**row, **replacement}, pin)


def test_default_engine_pair_continues_through_test_and_review(tmp_path, monkeypatch):
    workers(tmp_path, monkeypatch)
    install_mock_codex(tmp_path / "mock cli", monkeypatch, "approve")
    web, _ = project(tmp_path)
    repo = web.parent
    (web / "test_value.py").write_text(
        "from pathlib import Path\ndef test_accepted_patch():\n"
        "    assert (Path(__file__).parents[1] / 'answer.txt').read_text() == 'fixture'\n")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qam", "expect the approved patch"], check=True)
    with studio(tmp_path / "db") as local:
        status, info = request(local, "/api/workspaces", {"name": "기본 조합", "paths": [str(repo)]})
        assert status == 201
        run = uuid.uuid4().hex
        assert request(local, "/api/runs", {"id": run, "workspace_id": info["workspace"]["id"], "indices": [0],
                       "task": "원래 작업", "allow_edits": True, "use_inbox": True})[0] == 202
        question = wait_request(local, "/api", run, "question")
        assert question["engine"] == "codex"
        base = f"/api/runs/{run}/inbox"
        assert request(local, f"{base}/{question['id']}/decision", {"action": "answer", "answer": "둘째 방식"})[0] == 200
        permission = wait_request(local, "/api", run, "permission")
        assert "answer.txt" in permission["prompt"]
        assert request(local, f"{base}/{permission['id']}/decision", {"action": "approve"})[0] == 200
        result = wait_for(local, run, {"READY_FOR_HUMAN_REVIEW"})
        assert "2 passed" in result["test_output"] and "APPROVED" in result["review_output"]
        assert not (repo / "answer.txt").exists()
        assert [(r["decision"], r["status"]) for r in request(local, base)[1]["requests"]] == [
            ("answer", "delivered"), ("approve", "delivered")]


@pytest.mark.parametrize(("remote", "allow"), [(True, True), (False, False)])
def test_question_permission_continue_and_audit(tmp_path, monkeypatch, remote, allow):
    trace = install_inbox_fixture(tmp_path, monkeypatch)
    web, contracts = project(tmp_path)
    run = uuid.uuid4().hex
    with studio(tmp_path / "peer.db", accept_remote=True) as peer:
        host = RemoteHost("builder", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)
        lose = set()
        def forward(target, method, route, body):
            response = link(target, method, route, body)
            if route in lose:
                lose.remove(route)
                raise RemoteUnavailable()
            return response
        monkeypatch.setattr("agent_office.studio_server.remote_request", forward)
        api = "/api/hosts/" + host.journal_id if remote else "/api"
        with studio(tmp_path / "local.db", remotes=[host]) as local:
            status, workspace = request(local, api + "/workspaces", {"name": "주문", "paths": [str(web), str(contracts)]})
            assert status == 201
            job = {"id": run, "workspace_id": workspace["workspace"]["id"], "indices": [0, 1],
                   "task": "규격 적용", "allow_edits": allow, "developer_engine": "cline", "use_inbox": True}
            assert request(local, api + "/runs", job)[0] == 202
            assert request(local, api + "/runs", {**job, "use_inbox": False})[0] == 409
            q = wait_request(local, api, run, "question")
            route = f"/runs/{run}/inbox/{q['id']}/decision"
            assert request(local, api + route, {"action": "approve"})[0] == 400
            if remote:
                assert request(local, "/api/inbox")[1] == {"requests": []}
                lose.add(route)
            answer = {"action": "answer", "answer": "use spec v2"}
            assert request(local, api + route, answer)[0] == (503 if remote else 200)
            assert request(local, api + route, answer)[0] == 200
            perm = wait_request(local, api, run, "permission")
            assert perm["role"] == "developer" and perm["engine"] == "cline" and "value.txt" in perm["prompt"]
            decide = f"{api}/runs/{run}/inbox/{perm['id']}/decision"
            assert request(local, decide, answer)[0] == 400
            action = {"action": "approve" if allow else "reject"}
            assert request(local, decide, action)[0] == 200
            expected = "READY_FOR_HUMAN_REVIEW" if allow else "PLAN_ONLY"
            outcome = wait_for(local, run, {expected}, api=api)
            assert ("applied" if allow else "continued after refusal") in outcome["developer_output"]
            assert request(local, decide, action)[0] == 200
            assert request(local, decide, {"action": "reject" if allow else "approve"})[0] == 409
            status, audit = request(local, f"{api}/runs/{run}/inbox")
            assert status == 200
            assert [(r["id"], r["decision"], r["status"]) for r in audit["requests"]] == [
                (q["id"], "answer", "delivered"), (perm["id"], action["action"], "delivered")]
            assert all(r["decided_at"] and r["delivered_at"] for r in audit["requests"])
            assert len(trace.read_text().splitlines()) == 1 and (web / "value.txt").read_text() == "old"
            assert request(local, api + "/inbox")[1] == {"requests": []}

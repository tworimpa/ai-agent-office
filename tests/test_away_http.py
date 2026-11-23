"""Cross-process run delegation through the same Studio API used by the browser."""
import json
from io import BytesIO
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
import uuid
import pytest

from agent_office.away_expert import RULES
from agent_office.store import TaskStore
from agent_office.studio_ssh import RemoteHost, RemoteUnavailable, _read_response
from agent_office.workspace import create_worktree
from test_inbox_http import install_inbox_fixture, wait_request
from test_remote_studio import LoopbackLink
from test_studio_http import project, request, studio, wait_for
from test_studio_ssh import frame


POLICY = {"rules": [{"topic": "python_version", "path": "pyproject.toml"}],
          "specialist_engine": "claude", "verifier_engine": "antigravity"}


def test_worktree_uses_delegation_commit_when_original_head_advances(tmp_path):
    web, _ = project(tmp_path)
    repo = web.parent
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (web / "value.txt").write_text("later commit")
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qam", "later"],
                   cwd=repo, check=True)
    created = create_worktree(repo, revision=revision)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=created.path, text=True).strip() == revision
    assert (created.path / "web/value.txt").read_text() == "old"


def test_peer_cannot_add_or_silently_drop_a_delegation_on_receipt():
    pin, rid = uuid.uuid4().hex, uuid.uuid4().hex
    for requested, returned in [(None, POLICY), (POLICY, None), (POLICY, {**POLICY, "verifier_engine": "codex"})]:
        job = {"id": rid, "use_inbox": True, "delegation": requested}
        report = {"run": {**job, "state": "QUEUED", "delegation": returned}}
        with pytest.raises(RemoteUnavailable):
            _read_response(BytesIO(frame(json.dumps(report).encode(), 202, pin)), pin, "POST", "/runs", job)


def install(tmp_path, monkeypatch):
    install_inbox_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("OFFICE_FIXTURE_QUESTION", RULES["python_version"])
    monkeypatch.setenv("OFFICE_FIXTURE_ANSWER", ">=3.11")
    calls = tmp_path / "expert-calls"
    monkeypatch.setenv("OFFICE_AWAY_CALLS", str(calls))
    helper = Path(__file__).parent / "helpers" / "away_roles.py"
    for engine in ("claude", "agy"):
        launcher = tmp_path / "mock cli" / engine
        if os.name == "nt":
            launcher.with_suffix(".cmd").write_text(f'@"{sys.executable}" "{helper}" {engine} %*\n')
        else:
            launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} {engine} "$@"\n')
            launcher.chmod(0o700)
    web, contracts = project(tmp_path)
    (web.parent / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11"\n')
    subprocess.run(["git", "add", "pyproject.toml"], cwd=web.parent, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "policy fact"],
                   cwd=web.parent, check=True)
    return web, contracts, calls


def submit(local, api, web, contracts, policy=POLICY):
    status, data = request(local, api + "/workspaces", {"name": "주문", "paths": [str(web), str(contracts)]})
    assert status == 201, data
    job = {"id": uuid.uuid4().hex, "workspace_id": data["workspace"]["id"], "indices": [0, 1], "developer_engine": "cline",
           "allow_edits": False, "task": "런타임 확인", "use_inbox": True, "delegation": policy}
    assert request(local, api + "/runs", {**job, "use_inbox": False})[0] == 400
    status, data = request(local, api + "/runs", job)
    assert status == 202 and data["run"]["delegation"] == policy, data
    assert request(local, api + "/runs", job)[0] == 202
    assert request(local, api + "/runs", {**job, "delegation": None})[0] == 409
    return job["id"]


def test_remote_committed_fact_answers_only_the_question(tmp_path, monkeypatch):
    web, contracts, calls = install(tmp_path, monkeypatch)
    monkeypatch.setenv("OFFICE_FIXTURE_SOURCE", "delegate")
    db = tmp_path / "peer.db"
    with studio(db, accept_remote=True) as peer:
        host = RemoteHost("peer", peer.server_port, peer.journal_id)
        monkeypatch.setattr("agent_office.studio_server.remote_request", LoopbackLink(peer, host))
        api = "/api/hosts/" + peer.journal_id
        with studio(tmp_path / "local.db", remotes=[host]) as local:
            rid = submit(local, api, web, contracts)
            pending = wait_request(local, api, rid, "permission")
            status, office = request(local, api + "/office")
            assert status == 200, office
            assert {(card["role"], card["engine"], card["activity"]) for card in office["agents"]
                    if card["run_id"] == rid} == {("developer", "cline", "waiting")}
            status, report = request(local, api + "/return-report")
            assert status == 200 and report["counts"]["permissions"] == 1, report
            assert report["counts"]["questions"] == 0 and report["counts"]["delegated"] == 1
            row, = report["delegated"]
            assert row["run_id"] == rid and row["answer"] == ">=3.11" and row["evidence"]["line"] == 2
            assert row["evidence"]["path"] == "pyproject.toml"
            assert row["evidence"]["revision"] == subprocess.check_output(["git","rev-parse","HEAD"], cwd=web.parent, text=True).strip()
            assert request(local, "/api/return-report")[1]["counts"]["delegated"] == 0
            assert row["evidence"]["method"] == "committed_fact" and not calls.exists()
            assert row["specialist_engine"] is None and row["verifier_engine"] is None
            assert request(local, f"{api}/runs/{rid}/inbox/{pending['id']}/decision", {"action": "reject"})[0] == 200
            outcome = wait_for(local, rid, {"PLAN_ONLY"}, api=api)
            assert "continued after refusal" in outcome["developer_output"]
    with studio(db) as reopened:
        assert request(reopened, "/api/return-report")[1]["delegated"][0]["id"] == row["id"]
        _, history = request(reopened, f"/api/runs/{rid}/inbox")
        assert [(r["kind"], r["source"], r["decision"]) for r in history["requests"]] == [
            ("question", "delegate", "answer"), ("permission", "operator", "reject")]


@pytest.mark.parametrize("held_role", ["specialist", "verifier"])
def test_returning_operator_stops_active_consultation(tmp_path, monkeypatch, held_role):
    web, contracts, calls = install(tmp_path, monkeypatch)
    monkeypatch.setenv("OFFICE_FIXTURE_SOURCE", "operator")
    portfile = tmp_path / "held-role-port"
    monkeypatch.setenv("OFFICE_DELAY_" + held_role.upper(), str(portfile))
    db_path = tmp_path / "studio.db"

    def stages(request_id):
        with TaskStore(db_path, readonly=True) as store:
            return [tuple(stage) for stage in store.connection.execute(
                "SELECT role,engine,status FROM studio_away_role_stages WHERE request_id=? ORDER BY role",
                (request_id,))]

    with studio(db_path) as local:
        rid = submit(local, "/api", web, contracts, {**POLICY, "rules": [dict(POLICY["rules"][0], verification="independent")]})
        row = wait_request(local, "/api", rid, "question")
        previous = [] if held_role == "specialist" else [("specialist", "claude", "COMPLETED")]
        engine = POLICY[held_role + "_engine"]
        expected = previous + [(held_role, engine, "RUNNING")]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if portfile.exists() and portfile.read_text().strip() and stages(row["id"]) == expected:
                break
            time.sleep(0.05)
        port = int(portfile.read_text())
        assert stages(row["id"]) == expected

        def desk_activity():
            status, office = request(local, "/api/office")
            assert status == 200, office
            return {card["role"]: card["activity"] for card in office["agents"] if card["run_id"] == rid}

        activities = {"developer": "checking", held_role: "working"}
        if held_role == "verifier":
            activities["specialist"] = "done"
        assert desk_activity() == activities
        for _ in range(2):
            assert request(local, f"/api/runs/{rid}/delegation/stop", {}) == (200, {"run_id": rid, "active": False})
        assert desk_activity() == {**activities, "developer": "waiting", held_role: "stopped"}
        assert request(local, f"/api/runs/{rid}/inbox/{row['id']}/decision", {"action": "answer", "answer": ">=3.11"})[0] == 200
        permission = wait_request(local, "/api", rid, "permission")
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        assert request(local, f"/api/runs/{rid}/inbox/{permission['id']}/decision", {"action": "reject"})[0] == 200
        wait_for(local, rid, {"PLAN_ONLY"})
        assert request(local, "/api/return-report")[1]["counts"]["delegated"] == 0
        assert stages(row["id"]) == previous + [(held_role, engine, "INTERRUPTED")]
        assert [json.loads(line)[0] for line in calls.read_text().splitlines()] == [
            "AWAY_" + role.upper() for role, _, _ in stages(row["id"])]

"""Use the real coordinator and two native fixture programs, never vendor credentials."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

import pytest

from agent_office.engines import SUPPORTED_ENGINES
from agent_office.studio_runner import _request_digest
from agent_office.studio_ssh import RemoteHost
from test_remote_studio import LoopbackLink
from test_studio_http import project, request, studio, wait_for


def test_explicit_default_roles_match_historical_request_hash():
    parts = ["a" * 32, [1, 0], "한글 작업", False]
    previous = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
    assert _request_digest(parts, "codex", "antigravity") == previous
    assert _request_digest(parts, "gemini", "cline") != previous


def install_engine_pair(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fixture engines"
    bin_dir.mkdir()
    helper = Path(__file__).parent / "helpers" / "engine_pair.py"
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("OFFICE_FIXTURE_CALLS", str(calls))
    for name in ("gemini", "cline"):
        if os.name == "nt":
            (bin_dir / (name + ".cmd")).write_text(f'@"{sys.executable}" "{helper}" {name} %*\n')
        else:
            launcher = bin_dir / name
            launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} {name} "$@"\n')
            launcher.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return calls


@pytest.mark.parametrize(("remote", "developer", "reviewer"), [
    (False, "cline", "gemini"), (True, "gemini", "cline")
])
def test_selected_roles_execute_and_replay(tmp_path, monkeypatch, remote, developer, reviewer):
    calls = install_engine_pair(tmp_path, monkeypatch)
    web, contracts = project(tmp_path)
    run_id = uuid.uuid4().hex
    db = tmp_path / "workstation.db"
    with studio(tmp_path / "peer.db", accept_remote=True) as peer:
        host = RemoteHost("build", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)
        monkeypatch.setattr("agent_office.studio_server.remote_request", link)
        api = "/api/hosts/" + peer.journal_id if remote else "/api"
        with studio(db, remotes=[host]) as local:
            _, result = request(local, api + "/workspaces", {"name": "프로젝트", "paths": [str(web), str(contracts)]})
            job = {"id": run_id, "workspace_id": result["workspace"]["id"], "indices": [0, 1],
                   "task": "규격에 맞게 수정", "allow_edits": True, "developer_engine": developer, "reviewer_engine": reviewer}
            for key, value in (("developer_engine", "unknown"), ("reviewer_engine", [])):
                assert request(local, api + "/runs", {**job, key: value})[0] == 400
            assert request(local, api + "/runs")[1]["runs"] == []
            link.drop_receipt = remote
            assert request(local, api + "/runs", job)[0] == (503 if remote else 202)
            link.online = True
            assert request(local, api + "/runs", job)[0] == 202
            assert request(local, api + "/runs", {**job, "reviewer_engine": developer})[0] == 409
            run = wait_for(local, run_id, {"READY_FOR_HUMAN_REVIEW"}, api=api)
            assert run["developer_engine"] == developer and run["reviewer_engine"] == reviewer
            assert developer + " fixture edit" in run["developer_output"]
            assert reviewer + " fixture review" in run["review_output"]
            assert "+new" in run["diff"] and "2 passed" in run["test_output"]
            assert calls.read_text().splitlines() == [developer + " develop", reviewer + " review"]
            assert (web / "value.txt").read_text() == "old"
        with studio(db, remotes=[host]) as reopened:
            assert request(reopened, api + "/runs")[1]["runs"][0]["reviewer_engine"] == reviewer
            assert request(reopened, api + "/runs/" + run_id)[1]["run"]["developer_engine"] == developer
            if remote:
                assert request(reopened, "/api/runs")[1]["runs"] == []


def snapshot(version):
    return {"checked_at": "2026-11-19T01:00:00Z", "platform": "test", "engines": [
        {"engine": engine, "label": engine, "version": version, "installed": True, "probe_status": "ok",
         "auth_status": "unknown", "headless_support": True, "capabilities": {
             cap: {"status": "not_checked", "checked_at": None} for cap in ("read", "edit", "review")}}
        for engine in SUPPORTED_ENGINES]}


def test_capability_route_follows_pin_and_is_read_only(tmp_path, monkeypatch):
    from agent_office.studio_ssh import _read_response, RemoteUnavailable
    from test_studio_ssh import frame
    from io import BytesIO

    with studio(tmp_path / "remote.db", accept_remote=True) as peer:
        host = RemoteHost("peer", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)
        monkeypatch.setattr("agent_office.studio_server.remote_request", link)
        with studio(tmp_path / "local.db", remotes=[host]) as local:
            monkeypatch.setattr(peer.engines, "snapshot", lambda: snapshot("2.0"))
            monkeypatch.setattr(local.engines, "snapshot", lambda: snapshot("1.0"))
            remote = "/api/hosts/" + host.journal_id + "/engines"
            assert request(local, "/api/engines")[1] == snapshot("1.0")
            assert request(local, remote)[1] == snapshot("2.0")
            assert request(local, remote, {})[0] == 404
            assert len(link.calls) == 1
            def read(data, pin):
                stream = BytesIO(frame(json.dumps(data).encode(), journal=pin))
                return _read_response(stream, host.journal_id, "GET", "/engines", None)
            assert read(snapshot("2.0"), host.journal_id)[0] == 200
            with pytest.raises(RemoteUnavailable):
                read(snapshot("2.0"), local.journal_id)
            invalid = snapshot("2.0")
            invalid["engines"][0]["capabilities"]["edit"] = {"status": "passed", "checked_at": None}
            with pytest.raises(RemoteUnavailable):
                read(invalid, host.journal_id)


def test_ssh_receipt_confirms_requested_engine_assignment():
    from io import BytesIO
    from agent_office.studio_ssh import _read_response, RemoteUnavailable
    from test_studio_ssh import frame

    pin, run_id = uuid.uuid4().hex, uuid.uuid4().hex
    job = {"id": run_id, "developer_engine": "cline", "reviewer_engine": "gemini"}
    run = dict(job, state="QUEUED")
    for reported in (run, dict(run, reviewer_engine="codex")):
        body = {"run": reported}
        stream = BytesIO(frame(json.dumps(body).encode(), 202, pin))
        if reported == run:
            assert _read_response(stream, pin, "POST", "/runs", job) == (202, body)
        else:
            with pytest.raises(RemoteUnavailable):
                _read_response(stream, pin, "POST", "/runs", job)

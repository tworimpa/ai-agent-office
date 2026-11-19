from io import BytesIO
import json
import os
import sys
import time
import uuid

import pytest

from agent_office import cli, release_store, release_validation
from agent_office.studio_ssh import RemoteHost, RemoteUnavailable, _read_response, is_peer_route
from test_improvement_cli import OperatorTerminal, proposal_fixture
from test_remote_studio import LoopbackLink
from test_store import RUN
from test_studio_http import request as http, studio
from test_studio_ssh import frame


def test_remote_generation_publication_and_terminal_decision(tmp_path, monkeypatch, capsys, request):
    db, repo, base = proposal_fixture(tmp_path, monkeypatch)
    root, view = tmp_path / "release", tmp_path / "published" / "studio.sqlite3"
    view.parent.mkdir()
    mock_docker = os.environ.get("OFFICE_DOCKER_ACCEPTANCE") != "1"
    image = "sha256:" + "b" * 64 if mock_docker else request.getfixturevalue("trusted_validator_image")
    if mock_docker:
        def validate(git, sha, image):
            return dict(sha=sha, image=image, output="example regression passed", exit_code=0, passed=True)
        monkeypatch.setattr(release_validation, "validate_candidate", validate)
    release_store.init_release(root, repo, base, image)
    operator = ["--db", str(db), "improve", "--release", str(root)]
    def sync():
        assert cli.main([*operator, "studio-sync", "--view", str(view)]) == 0, capsys.readouterr()

    with studio(db, improvement_source=repo, release_view=view, accept_remote=True) as peer:
        host = RemoteHost("work-host", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)
        def relay(target, method, route, payload):
            status, data = link(target, method, route, payload)
            return _read_response(BytesIO(frame(json.dumps(data).encode(), status, peer.journal_id)),
                                  host.journal_id, method, route, payload)
        monkeypatch.setattr("agent_office.studio_server.remote_request", relay)
        with studio(tmp_path / "desktop.db", remotes=[host]) as desktop:
            api = "/api/hosts/" + peer.journal_id + "/improvements"
            job_id = uuid.uuid4().hex
            command = dict(id=job_id, engine="claude", base=base, run_ids=[RUN])
            def get(route):
                code, data = http(desktop, api + route)
                assert code == 200, data
                return data
            assert http(desktop, "/improvements?host=" + peer.journal_id)[0] == 200
            assert get("")["view"]["available"] is False
            assert http(desktop, api + "/jobs", command)[0] == 400
            sync()
            assert get("")["view"]["stable"] == base
            assert get("/runs/page/0")["items"][0]["id"] == RUN
            assert http(desktop, "/api/improvements/runs/page/0")[1]["items"] == []
            assert http(desktop, api + "/jobs", command)[0] == 202
            link.online = False
            assert http(desktop, api + "/jobs/" + job_id)[0] == 503
            link.online = True
            assert http(desktop, api + "/jobs", command)[1]["job"]["id"] == job_id
            assert http(desktop, api + "/jobs", dict(command, engine="gemini"))[0] == 409
            deadline = time.monotonic() + 45
            while True:
                detail = get("/jobs/" + job_id)
                if detail["job"]["state"] != "generating":
                    break
                assert time.monotonic() < deadline, detail
                time.sleep(.15)
            assert detail["job"]["state"] == "ready", detail
            assert "+after" in detail["proposal"]["patch"] and detail["candidate_id"] is None
            sync()
            candidate_id = get("/jobs/" + job_id)["candidate_id"]
            detail = get("/candidates/" + candidate_id)["candidate"]
            assert detail["state"] == "passed" and detail["validation"] == "passed"
            assert detail["job_id"] == job_id and detail["exit_code"] == 0 and "+after" in detail["diff"]
            assert detail["decision"] is None and get("")["view"]["stable"] == base
            candidate_route = f"/api/peer/{host.journal_id}/improvements/candidates/{candidate_id}"
            assert "stale_base" not in http(peer, candidate_route)[1]["candidate"]
            with_basis = http(peer, candidate_route, **{"X-Agent-Office-Candidate-Basis": "1"})
            assert with_basis[1]["candidate"]["stale_base"] is False
            moved = view.rename(view.with_suffix(".offline"))
            try:
                assert not get("")["view"]["available"]
                job = get("/jobs/" + job_id)
                assert job["publication_available"] is False and "+after" in job["proposal"]["patch"]
                assert http(desktop, api + "/candidates/page/0")[0] == 503
                assert http(desktop, api + "/jobs", command)[0] == 202
            finally:
                moved.rename(view)
            for suffix in ("approve", "reject", "verify", "checkout-stable"):
                assert http(desktop, api + "/candidates/" + candidate_id + "/" + suffix, {})[0] == 404
            sha = detail["sha"]
            with monkeypatch.context() as m:
                m.setattr(sys, "stdin", OperatorTerminal("approve " + sha + "\n"))
                m.setattr(sys, "stderr", OperatorTerminal())
                assert cli.main([*operator, "approve", candidate_id, "--sha", sha]) == 0
            sync()
            assert get("")["view"]["stable"] == sha
            assert get("/candidates/page/0")["items"][0]["decision"] == "approved"
            assert get("/candidates/" + candidate_id)["candidate"]["history"][-1]["event"] == "approved"
            assert http(desktop, api + "/jobs", command)[0] == 202
            assert http(desktop, api + "/jobs", dict(command, id=uuid.uuid4().hex))[0] == 400


def test_improvements_are_bound_to_host_method_and_exact_job(tmp_path):
    with studio(tmp_path / "desk.db") as desktop:
        for route in ("/improvements", "/improvements?host=local", "/improvements?candidate=" + "b"*32,
                      "/improvements.js", "/improvements.css"):
            assert http(desktop, route)[0] == 200
        for query in ("host=", "host=x", "job=oops", "job=" + "a"*32 + "&candidate=" + "c"*32):
            assert http(desktop, "/improvements?" + query)[0] == 400
        for route in ("/improvements/approve", "/improvements/candidates", "/improvements/jobs/", "/improvements/status",
                      "/improvements/jobs/" + "c"*32 + "/decision"):
            assert not is_peer_route("POST", route)
            assert http(desktop, "/api" + route, {})[0] == 404
    pin, jid = uuid.uuid4().hex, uuid.uuid4().hex
    body = dict(id=jid, base="b"*40, engine="codex", run_ids=[RUN])
    job = dict(body, title="", message="", state="generating")
    def decode(job, journal=pin):
        wire = BytesIO(frame(json.dumps({"job": job}).encode(), 202, journal))
        return _read_response(wire, pin, "POST", "/improvements/jobs", body)
    assert decode(job)[0] == 202
    for bad in (dict(job, run_ids=["e"*32]), dict(job, engine="claude"), dict(job, id="c"*32)):
        with pytest.raises(RemoteUnavailable):
            decode(bad)
    with pytest.raises(RemoteUnavailable):
        decode(job, uuid.uuid4().hex)


def test_peer_cannot_report_a_failed_candidate_as_passed():
    pin, cid = uuid.uuid4().hex, uuid.uuid4().hex
    row = dict(id=cid, job_id=None, base="a"*40, sha="b"*40, title="Test report",
               validation="passed", decision=None, state="failed")
    page = {"items": [row], "next_offset": None}
    def read():
        return _read_response(BytesIO(frame(json.dumps(page).encode(), 200, pin)),
                              pin, "GET", "/improvements/candidates/page/0", None)
    with pytest.raises(RemoteUnavailable):
        read()
    row["state"] = "submitted"
    with pytest.raises(RemoteUnavailable):
        read()
    row["state"] = "passed"
    assert read()[1] == page

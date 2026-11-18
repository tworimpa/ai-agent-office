"""A workstation and a peer use separate journals and real fixture worker processes."""
from pathlib import Path
import subprocess
import time
import uuid

import pytest

from agent_office.studio_ssh import RemoteHost, RemoteUnavailable
from test_studio_http import project, request, studio, wait_for, workers


class LoopbackLink:
    """Replace SSH with interruptible HTTP to the peer's real request handler."""

    def __init__(self, peer, host):
        self.peer = peer
        self.host = host
        self.online = True
        self.drop_receipt = False
        self.calls = []

    def __call__(self, host, method, route, payload=None):
        assert host == self.host
        assert method == ("GET" if payload is None else "POST")
        self.calls.append((method, route))
        if not self.online:
            raise RemoteUnavailable()
        response = request(self.peer, f"/api/peer/{host.journal_id}{route}", payload)
        if self.drop_receipt and method == "POST" and route == "/runs" and response[0] == 202:
            self.drop_receipt = False
            self.online = False
            raise RemoteUnavailable()
        return response


def assert_unavailable(response):
    status, body = response
    assert status == 503, body
    assert body["error"]["code"] == "REMOTE_UNAVAILABLE"


def test_cli_registers_explicit_peers_without_opening_a_connection(tmp_path, monkeypatch):
    from agent_office.cli import main

    journal = uuid.uuid4().hex
    captured = []
    monkeypatch.setattr("agent_office.studio_server.serve_studio",
                        lambda db, port, **kwargs: captured.append((db, port, kwargs)))
    args = ["--db", str(tmp_path / "local.db"), "studio", "--port", "0", "--accept-remote",
            "--remote", "build-box", "8766", journal]
    assert main(args) == 0
    assert captured == [(tmp_path / "local.db", 0, {
        "remotes": [RemoteHost("build-box", 8766, journal)], "accept_remote": True,
    })]
    assert main([*args[:-2], "0", journal]) == 2
    assert len(captured) == 1


def wait_for_file(path):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    pytest.fail(f"Fixture did not reach {path}")


def test_lost_receipt_restart_reconnect_and_remote_stop(tmp_path, monkeypatch):
    gate = tmp_path / "allow-edit"
    workers(tmp_path, monkeypatch, edit_gate=gate)
    remote_dir = tmp_path / "peer"
    web, contracts = project(remote_dir)
    local_db = tmp_path / "workstation" / "tasks.db"
    repo = web.parent

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    origin = remote_dir / "origin.git"
    git("clone", "--quiet", "--bare", str(repo), str(origin))
    git("remote", "add", "origin", str(origin))
    original_refs = git("ls-remote", "origin")
    original_head = git("rev-parse", "HEAD")
    run_id = uuid.uuid4().hex
    with studio(remote_dir / "tasks.db", accept_remote=True) as peer:
        host = RemoteHost("test-peer", peer.server_port, peer.journal_id)
        api = f"/api/hosts/{peer.journal_id}"
        link = LoopbackLink(peer, host)
        monkeypatch.setattr("agent_office.studio_server.remote_request", link)
        with studio(local_db, remotes=(host,)) as local:
            assert request(local, "/api/hosts") == (200, {"hosts": [
                {"id": "local", "name": "이 컴퓨터"}, {"id": peer.journal_id, "name": "test-peer"}
            ]})
            status, data = request(local, f"{api}/workspaces", {
                "name": "원격 작업공간", "paths": [str(web), str(contracts)],
            })
            assert status == 201, data
            registration = data["workspace"]
            assert request(local, f"{api}/workspaces")[1]["workspaces"] == [registration]
            job = {"id": run_id, "workspace_id": registration["id"], "indices": [0, 1],
                   "task": "두 폴더에 규격 적용", "allow_edits": True}
            link.drop_receipt = True
            assert_unavailable(request(local, f"{api}/runs", job))
            wait_for_file(gate.with_suffix(".calls"))
            assert request(peer, f"/api/runs/{run_id}")[1]["run"]["state"] == "RUNNING"
            assert request(local, "/api/runs")[1]["runs"] == []

        with studio(local_db, remotes=(host,)) as reopened:
            assert_unavailable(request(reopened, f"{api}/runs/{run_id}"))
            assert_unavailable(request(reopened, f"{api}/runs", job))
            assert request(reopened, "/api/workspaces")[1]["workspaces"] == []
            assert request(reopened, "/api/runs")[1]["runs"] == []
            link.online = True
            status, receipt = request(reopened, f"{api}/runs", job)
            assert status == 202 and receipt["run"]["id"] == run_id
            assert receipt["run"]["state"] == "RUNNING"
            assert request(reopened, f"{api}/runs", {**job, "task": "다른 요청"})[0] == 409

            link.online = False
            gate.touch()
            finished = wait_for(peer, run_id, {"READY_FOR_HUMAN_REVIEW"})
            link.online = True
            assert request(reopened, f"{api}/runs/{run_id}")[1]["run"] == finished
            assert [run["id"] for run in request(reopened, f"{api}/runs")[1]["runs"]] == [run_id]
            assert gate.with_suffix(".calls").read_text().splitlines() == ["edit"]
            assert finished["folders"] == ["web", "contracts"]
            assert "fixture edit" in finished["developer_output"]
            assert "2 passed" in finished["test_output"]
            assert "VERDICT: APPROVED" in finished["review_output"]
            assert "web/value.txt" in finished["changes"] and "contracts/result.txt" in finished["changes"]
            assert "+new" in finished["diff"] and "+contract applied" in finished["diff"]
            worktree = Path(finished["workspace"])
            assert worktree.is_relative_to(remote_dir) and worktree != repo
            assert (worktree / "web" / "value.txt").read_text() == "new"
            assert (worktree / "contracts" / "result.txt").read_text() == "contract applied"
            assert git("rev-parse", "HEAD") == original_head and git("status", "--porcelain") == ""
            assert git("ls-remote", "origin") == original_refs

            stopped_id = uuid.uuid4().hex
            assert request(reopened, f"{api}/runs", {**job, "id": stopped_id, "task": "WAIT_FOR_STOP"})[0] == 202
            running = wait_for(reopened, stopped_id, {"RUNNING"}, api=api)
            wait_for_file(Path(running["workspace"]) / "started")
            status, result = request(reopened, f"{api}/runs/{stopped_id}/stop", {})
            assert status == 200 and result["run"]["state"] in {"STOP_REQUESTED", "CANCELED"}
            wait_for(reopened, stopped_id, {"CANCELED"}, api=api)
            assert request(peer, f"/api/runs/{stopped_id}")[1]["run"]["state"] == "CANCELED"
            assert request(reopened, "/api/runs")[1]["runs"] == []


@pytest.mark.parametrize(("enabled", "correct_id", "error"), [
    (False, True, (403, "PEER_DISABLED")), (True, False, (409, "JOURNAL_MISMATCH")),
])
def test_peer_opt_in_and_pinned_journal(tmp_path, monkeypatch, enabled, correct_id, error):
    remote_db = tmp_path / "peer.db"
    with studio(remote_db) as prior:
        journal = prior.journal_id
    with studio(remote_db, accept_remote=enabled) as peer:
        assert peer.journal_id == journal
        pin = journal if correct_id else uuid.uuid4().hex
        host = RemoteHost("test-peer", peer.server_port, pin)
        link = LoopbackLink(peer, host)
        monkeypatch.setattr("agent_office.studio_server.remote_request", link)
        registration = {"name": "거부될 작업공간", "paths": [str(tmp_path)]}
        status, detail = request(peer, f"/api/peer/{pin}/workspaces", registration)
        assert (status, detail["error"]["code"]) == error
        with studio(tmp_path / "local.db", remotes=(host,)) as local:
            status, detail = request(local, f"/api/hosts/{pin}/workspaces", registration)
            assert (status, detail["error"]["code"]) == (503, error[1])
            assert request(local, "/api/workspaces")[1]["workspaces"] == []
            assert request(peer, "/api/workspaces")[1]["workspaces"] == []


def test_only_configured_hosts_and_studio_routes_reach_the_peer(tmp_path, monkeypatch):
    with studio(tmp_path / "peer.db", accept_remote=True) as peer:
        host = RemoteHost("test-peer", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)
        monkeypatch.setattr("agent_office.studio_server.remote_request", link)
        with studio(tmp_path / "local.db", remotes=(host,)) as local:
            api = f"/api/hosts/{peer.journal_id}"
            status, payload = request(local, f"/api/hosts/{uuid.uuid4().hex}/runs")
            assert status == 404 and payload["error"]["code"] == "UNKNOWN_HOST"
            for route in ("/hosts", "/assets", "/runs/invalid-id", "/runs?extra=1", "/runs/../workspaces"):
                assert request(local, api + route)[0] in {400, 404}
            assert request(local, f"{api}/workspaces", {}, Origin="null")[0] == 403
            assert link.calls == []

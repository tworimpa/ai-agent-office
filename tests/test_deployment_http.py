from io import BytesIO
import json
from pathlib import Path
import select
import socket

import pytest

from agent_office import deployment, release_store
from agent_office.release_view import sync_once
from agent_office.studio_ssh import RemoteHost, RemoteUnavailable, _read_response, is_peer_route
from test_release_store import IMAGE, source
from test_remote_studio import LoopbackLink
from test_studio_http import request, studio
from test_studio_ssh import frame


def test_selected_host_observes_process_identity_and_matches_its_publication(tmp_path, source, monkeypatch):
    release = tmp_path / "release"
    stable = release_store.init_release(release, source, "HEAD", IMAGE)["stable"]
    with release_store._db(release) as db:
        release_id = db.execute("SELECT id FROM studio_release_identity").fetchone()[0]
    peer_db = tmp_path / "peer.db"
    view, target = tmp_path / "published" / "view.db", tmp_path / "target"
    calls = []
    status = {"service": "office-test", "journal_id": "f"*32, "release_id": release_id, "last": None, "health": "healthy",
              "actual": {"seq": 1, "sha": stable, "boot_id": "a"*32}}

    def observe(path):
        assert Path(path) == target
        calls.append(path)
        return status

    monkeypatch.setattr(deployment, "public_status", observe)
    with studio(peer_db, release_view=view, deployment_target=target, accept_remote=True) as peer:
        sync_once(peer_db, release, view, stable_history=True)
        remote = RemoteHost("deployment-test", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, remote)

        def relay(host, method, route, payload):
            code, data = link(host, method, route, payload)
            return _read_response(BytesIO(frame(json.dumps(data).encode(), code, host.journal_id)),
                                  host.journal_id, method, route, payload)

        monkeypatch.setattr("agent_office.studio_server.remote_request", relay)
        with studio(tmp_path / "client.db", remotes=[remote]) as desk:
            api = f"/api/hosts/{remote.journal_id}/deployment"
            selected = f"/versions/1/{stable}"
            assert request(desk, "/api/deployment")[1]["health"] == "unconfigured"
            assert not calls
            assert request(desk, api)[1]["release_matches"] is None
            code, body = request(desk, api + selected)
            assert code == 200 and body["release_matches"] and body["running"] == status["actual"]
            assert body["runtime_journal_id"] == "f"*32 and peer.journal_id != body["runtime_journal_id"]
            status["release_id"] = "d"*32
            assert request(desk, api + selected)[1]["release_matches"] is False
            status["release_id"] = release_id
            assert not request(desk, api + "/versions/1/" + "c"*40)[1]["release_matches"]
            status.update(health="offline", actual=None)
            code, body = request(desk, api + selected)
            assert code == 200 and body["running"] is None and body["health"] == "offline"
            link.online = False
            assert request(desk, api)[0] == 503
            link.online = True
            sync_once(peer_db, release, view)
            assert request(desk, api + selected)[1]["release_matches"] is None
            count = len(calls)
            for suffix in ("", "/restart", "/recover", selected):
                assert request(desk, api + suffix, {})[0] == 404
                assert request(peer, "/api/deployment" + suffix, {})[0] == 404
            assert len(calls) == count


def test_an_old_or_mismatched_probe_cannot_satisfy_a_selected_version_request():
    pin, sha = "a"*32, "b"*40
    route = "/deployment/versions/1/" + sha
    data = dict(configured=True, service="office-test", runtime_journal_id=pin, release_matches=True, health="healthy",
                selection={"seq": 1, "sha": sha}, last=None, running={"seq": 1, "sha": sha, "boot_id": "c"*32})
    def read(reply, journal=pin):
        return _read_response(BytesIO(frame(json.dumps(reply).encode(), 200, journal)), pin, "GET", route, None)
    assert read(data)[1] == data
    for invalid in (dict(data, selection=None), dict(data, selection={"seq": 2, "sha": sha}),
                    dict(data, health="offline"), dict(data, running=None), dict(data, release_matches=1)):
        with pytest.raises(RemoteUnavailable):
            read(invalid)
    restored = dict(id="f"*32, state="deploy_failed_restored", requested={"seq": 1, "sha": sha},
                    previous={"seq": 2, "sha": "d"*40}, time="2026-11-19T13:00:00+00:00", message="restored")
    with pytest.raises(RemoteUnavailable):
        read(dict(data, last=restored))
    with pytest.raises(RemoteUnavailable):
        read(data, "d"*32)
    for invalid in ("/deployment/versions/0/" + sha, "/deployment?port=8766", "/deployment/restart",
                    "/deployment/versions/1/HEAD"):
        assert not is_peer_route("GET", invalid)
    assert not is_peer_route("POST", "/deployment")


def test_rejected_deployment_post_receives_a_complete_http_response(tmp_path):
    from http.client import HTTPResponse

    with studio(tmp_path / "desk.db") as desk, socket.create_connection(desk.server_address, timeout=5) as client:
        host = f"127.0.0.1:{desk.server_port}"
        header = (f"POST /api/deployment/restart HTTP/1.0\r\nHost: {host}\r\nOrigin: http://{host}\r\n"
                  "X-Agent-Office: studio\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n")
        client.sendall(header.encode("ascii"))
        assert not select.select([client], [], [], 0.2)[0]
        client.sendall(b"{}")
        with HTTPResponse(client) as response:
            response.begin()
            assert response.status == 404 and json.loads(response.read())["error"]["code"] == "NOT_FOUND"

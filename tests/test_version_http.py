from io import BytesIO
import json
import sys
import uuid

import pytest

from agent_office import cli, release_store, release_validation
from agent_office.studio_ssh import RemoteHost, RemoteUnavailable, _read_response, is_peer_route
from test_improvement_cli import OperatorTerminal
from test_release_store import IMAGE, proposal, result, source
from test_remote_studio import LoopbackLink
from test_studio_http import request, studio
from test_studio_ssh import frame


def test_studio_and_peer_read_published_versions_across_terminal_rollback(tmp_path, source, monkeypatch, capsys):
    root, journal, view = tmp_path / "release", tmp_path / "peer.db", tmp_path / "published" / "view.db"
    initial = release_store.init_release(root, source, "HEAD", IMAGE)["stable"]
    monkeypatch.setattr(release_validation, "validate_candidate", lambda g, s, i: result(s, i))
    op = ["--db", str(journal), "improve", "--release", str(root)]
    def publish(*options):
        assert cli.main([*op, "studio-sync", "--view", str(view), *options]) == 0, capsys.readouterr()
    with studio(journal, release_view=view, accept_remote=True) as peer:
        host = RemoteHost("release-host", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)
        def relay(target, method, route, body):
            code, data = link(target, method, route, body)
            return _read_response(BytesIO(frame(json.dumps(data).encode(), code, host.journal_id)),
                                  host.journal_id, method, route, body)
        monkeypatch.setattr("agent_office.studio_server.remote_request", relay)
        with studio(tmp_path / "desktop.db", remotes=[host]) as desktop:
            prefix = f"/api/hosts/{peer.journal_id}/versions"
            def get(route):
                code, data = request(desktop, prefix + route)
                assert code == 200, data
                return data
            publish()
            assert get("/page/0")["available"] is False
            publish("--stable-history")
            assert get("/page/0")["current"]["sha"] == initial
            new = release_store.submit(root, proposal(initial))
            release_store.verify_candidate(root, new["id"])
            publish("--stable-history")
            assert get("/page/0")["current"]["seq"] == 1
            assert request(desktop, prefix + "/compare/1/2")[0] == 404
            release_store.decide(root, new["id"], new["sha"], "approve")
            publish("--stable-history")
            page = get("/page/0")
            assert page["current"] == page["items"][0] and page["current"]["seq"] == 2
            assert request(peer, "/api/versions/page/0")[1] == page
            assert request(desktop, "/api/versions/page/0")[1]["available"] is False
            assert "+new" in get("/compare/1/2")["diff"]
            undo = get("/compare/2/1")
            assert "+old" in undo["diff"] and undo["files"][0]["path"] == "example.txt"
            for route in ("/rollback/1", "/compare/2/1", "/page/0"):
                assert request(desktop, prefix + route, {})[0] == 404
                assert request(peer, "/api/versions" + route, {})[0] == 404
            assert cli.main([*op, "rollback", "1"]) != 0
            assert release_store.release_status(root)["stable"] == new["sha"]
            with monkeypatch.context() as m:
                phrase = f"rollback 2 {new['sha']} 1 {initial}\n"
                m.setattr(sys, "stdin", OperatorTerminal(phrase))
                screen = OperatorTerminal()
                m.setattr(sys, "stderr", screen)
                assert cli.main([*op, "rollback", "1"]) == 0, screen.getvalue()
                assert "+old" in screen.getvalue()
            link.online = False
            publish("--stable-history")
            assert request(desktop, prefix + "/page/0")[0] == 503
            link.online = True
            page = get("/page/0")
            assert [e["kind"] for e in page["items"]] == ["rollback", "promotion", "initial"]
            assert page["current"]["sha"] == initial and page["current"]["target_seq"] == 1
            assert get("/compare/1/3")["total_files"] == 0
            restored = get("/compare/2/3")
            assert restored["files"] == undo["files"] and "+old" in restored["diff"]
            dest = tmp_path / "stable-export"
            assert release_store.checkout_stable(root, dest)["stable"] == initial
            assert (dest / "example.txt").read_bytes() == b"old\n"
            publish()
            assert get("/page/0")["available"] is False
            assert request(desktop, prefix + "/compare/2/3")[0] != 200


def test_versions_navigation_and_route_allowlist(tmp_path):
    peer = "a" * 32
    with studio(tmp_path / "studio.db") as server:
        for path in ("/versions", "/versions?host=local", "/versions?host=" + peer, "/versions.js", "/versions.css"):
            assert request(server, path)[0] == 200
        for path in ("/", "/improvements", "/office"):
            assert b"/versions" in request(server, path)[1]
        for query in ("run=123", "host=", "host=nope", "host=local&host=local"):
            assert request(server, "/versions?" + query)[0] == 400
    for route in ("/versions/rollback", "/versions/compare/0/1", "/versions/page/1",
                  "/versions/compare/1/HEAD", "/versions/compare/01/1", "/versions/compare/1/9007199254740992"):
        assert not is_peer_route("GET", route)
    assert not is_peer_route("POST", "/versions/compare/2/1")


def test_history_wire_rejects_contradictory_events_and_comparisons():
    pin = uuid.uuid4().hex
    first = dict(seq=1, kind="initial", time=None, previous=None, sha="a"*40, target_seq=None)
    stamp = "2026-11-19T12:21:23+00:00"
    second = dict(seq=2, kind="promotion", previous=first["sha"], sha="b"*40, target_seq=None, time=stamp)
    third = dict(seq=3, kind="rollback", previous=second["sha"], sha=first["sha"], target_seq=1, time=stamp)
    page = dict(current=third, items=[third, second, first], next_offset=None, updated_at=stamp, available=True)
    def read(doc, route, journal=pin):
        return _read_response(BytesIO(frame(json.dumps(doc).encode(), 200, journal)), pin, "GET", route, None)
    route = "/versions/page/0"
    assert read(page, route)[1] == page
    for value in (dict(page, current=first), dict(page, items=[third, first]), dict(page, available=False),
                  dict(page, items=[third, dict(second, sha="c"*40), first])):
        with pytest.raises(RemoteUnavailable):
            read(value, route)
    with pytest.raises(RemoteUnavailable):
        read(page, route, "d"*32)
    empty = dict(from_seq=1, to_seq=3, from_sha=first["sha"], to_sha=third["sha"],
                 total_files=0, files=[], diff="", truncated=False)
    assert read(empty, "/versions/compare/1/3")[1] == empty
    for value in (dict(empty, from_seq=True), dict(empty, total_files=2), dict(empty, diff="wrong")):
        with pytest.raises(RemoteUnavailable):
            read(value, "/versions/compare/1/3")

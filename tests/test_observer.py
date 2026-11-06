from contextlib import contextmanager
from dataclasses import asdict
import http.client
import json
from pathlib import Path
import threading
import os
import socket
from email.message import Message
from types import SimpleNamespace
import pytest
from test_store import seeded, TASK, RUN


@contextmanager
def running(db):
    from agent_office.observer import ObserverServer
    with ObserverServer(db, port=0, engine_snapshot={"checked_at":"2026-11-06T00:00:00Z","platform":"test","engines":[]}) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


def request(server, path, method="GET", headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_http_projection_reads_existing_database_only(tmp_path, monkeypatch):
    db = tmp_path / "한글 space" / "db"
    with seeded(db) as store:
        expected = asdict(store.get_task(TASK))
    before = db.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("Observer attempted execution")
    monkeypatch.setattr("agent_office.adapters.CLIAdapter.execute", forbidden)
    monkeypatch.setattr("agent_office.workspace.create_worktree", forbidden)
    monkeypatch.setattr("agent_office.ownership.recover_run", forbidden)
    with running(db) as server:
        for path in ("/api/tasks", f"/api/tasks/{TASK}", f"/api/tasks/{TASK}/events", "/api/meta"):
            status, headers, body = request(server, path)
            assert status == 200
            payload = json.loads(body)
            assert headers["Cache-Control"] == "no-store"
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
            assert "Access-Control-Allow-Origin" not in headers
            if path == f"/api/tasks/{TASK}":
                assert payload["task"] == expected
        assert db.read_bytes() == before
        assert server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize("path,status", [("/missing",404), ("/api/tasks/bad",400),
    (f"/api/tasks/{'f'*32}",404), ("/api/tasks?db=anything",400),
    (f"/api/tasks/{TASK}/events?after_seq=-1",400),
    (f"/api/tasks/{TASK}/events?limit=501",400),
    (f"/api/tasks/{TASK}/events?limit=1&limit=2",400),
    (f"/api/tasks/{TASK}/events?run_id=",400),
    (f"/api/tasks/{TASK}/events?after_seq={2**63}",400),
    ("/../pyproject.toml",404), ("/%2e%2e/pyproject.toml",404)])
def test_invalid_routes_and_queries_are_rejected(tmp_path, path, status):
    db = tmp_path / "db"
    seeded(db).close()
    with running(db) as server:
        actual, _, body = request(server, path)
        assert actual == status
        assert "error" in json.loads(body)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_mutation_methods_are_unavailable(tmp_path, method):
    db = tmp_path / "db"
    seeded(db).close()
    before = db.read_bytes()
    with running(db) as server:
        status, headers, _ = request(server, "/api/tasks", method)
        assert status == 405 and headers["Allow"] == "GET, HEAD"
    assert db.read_bytes() == before


@pytest.mark.parametrize("headers", [{"Host":"evil.example:123"}, {"Origin":"https://evil.example"}, {"Sec-Fetch-Site":"cross-site"}])
def test_foreign_api_requests_are_forbidden(tmp_path, headers):
    db = tmp_path / "db"
    seeded(db).close()
    with running(db) as server:
        assert request(server, "/api/tasks", headers=headers)[0] == 403


def test_cursor_and_head_and_database_disappearance(tmp_path):
    db = tmp_path / "db"
    seeded(db).close()
    with running(db) as server:
        status, headers, body = request(server, "/api/tasks", "HEAD")
        assert status == 200 and not body and int(headers["Content-Length"]) > 0
        payload = json.loads(request(server, f"/api/tasks/{TASK}/events?after_seq=1")[2])
        assert payload["events"] == [] and payload["next_after_seq"] == 1
        db.unlink()
        status, _, body = request(server, "/api/tasks")
        assert status == 503 and json.loads(body)["error"]["code"] == "DB_NOT_FOUND"
        assert not db.exists()


def test_missing_database_does_not_start_server_or_create_file(tmp_path):
    from agent_office.observer import ObserverServer
    path = tmp_path / "missing" / "db"
    with pytest.raises(Exception) as error:
        ObserverServer(path, port=0)
    assert error.value.code == "DB_NOT_FOUND"
    assert not path.parent.exists()


def test_static_assets_are_packaged_and_not_filesystem_browsing(tmp_path):
    db = tmp_path / "db"
    seeded(db).close()
    with running(db) as server:
        for path, mime in (("/", "text/html"), ("/style.css", "text/css"), ("/app.js", "text/javascript"), ("/state.js", "text/javascript")):
            status, headers, body = request(server, path)
            assert status == 200 and body
            assert headers["Content-Type"].startswith(mime)
        assert request(server, "/web/../../cli.py")[0] == 404


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_root_task_link_survives_http_refresh(tmp_path, method):
    db = tmp_path / "db"
    seeded(db).close()
    with running(db) as server:
        status, headers, body = request(server, f"/?task={TASK}", method)
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        if method == "GET":
            assert b'<!doctype html>' in body
        else:
            assert body == b''
        assert request(server, f"/?task={TASK}&task={TASK}")[0] == 400
        assert request(server, "/?task=bad")[0] == 400


def test_replaced_database_is_not_silently_adopted(tmp_path):
    db, replacement = tmp_path / "db", tmp_path / "replacement"
    seeded(db).close()
    with seeded(replacement) as store:
        store.connection.execute("UPDATE tasks SET request_snapshot='replacement data'")
    with running(db) as server:
        assert request(server, "/api/tasks")[0] == 200
        os.replace(replacement, db)
        for path in ("/api/meta", "/api/tasks", f"/api/tasks/{TASK}", f"/api/tasks/{TASK}/events"):
            status, _, body = request(server, path)
            assert status == 503
            assert b'replacement data' not in body


def test_corrupt_stored_event_is_a_service_error(tmp_path):
    db = tmp_path / "db"
    with seeded(db) as store:
        store.connection.execute("UPDATE events SET payload_json='invalid JSON'")
    with running(db) as server:
        status, headers, body = request(server, f"/api/tasks/{TASK}/events")
        assert status == 503
        assert json.loads(body)["error"]["code"] == "STORE_IO"
        assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("host,origin,allowed", [("127.0.0.1","http://127.0.0.1",True),
    ("localhost","http://localhost",True), ("127.0.0.1:80","http://127.0.0.1",True),
    ("evil.example","http://evil.example",False), ("localhost:81","http://localhost:81",False)])
def test_default_http_port_uses_browser_normalized_headers(host, origin, allowed):
    from agent_office.observer import ObserverHandler
    handler = object.__new__(ObserverHandler)
    handler.server = SimpleNamespace(server_address=("127.0.0.1",80))
    handler.headers = Message()
    handler.headers["Host"] = host
    handler.headers["Origin"] = origin
    assert handler._allowed(True) is allowed


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "UNSUPPORTED"])
def test_unknown_methods_have_fixed_error_contract(tmp_path, method):
    db = tmp_path / "db"
    seeded(db).close()
    with running(db) as server:
        status, headers, body = request(server, "/api/tasks", method)
        assert status == 405
        assert headers["Allow"] == "GET, HEAD"
        assert headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert json.loads(body)["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_unsupported_http_version_is_not_reported_as_database_failure(tmp_path):
    db = tmp_path / "db"
    seeded(db).close()
    before = db.read_bytes()
    with running(db) as server:
        with socket.create_connection(server.server_address, timeout=5) as client:
            client.sendall(b"GET / HTTP/2.0\r\nHost: localhost\r\n\r\n")
            chunks = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        response = b"".join(chunks)
        body = response.partition(b"\r\n\r\n")[2] if response.startswith(b"HTTP/") else response
        assert json.loads(body)["error"]["code"] == "INVALID_ARGUMENT"
    assert db.read_bytes() == before


def test_engine_snapshot_is_collected_once_not_on_page_poll(tmp_path,monkeypatch):
    from agent_office.observer import ObserverServer
    snapshot={"checked_at":"2026-11-06T00:00:00Z","platform":"test","engines":[]}
    calls=[]
    def collect():
        calls.append(1)
        return snapshot
    monkeypatch.setattr('agent_office.engine_status.collect_engine_status',collect)
    db=tmp_path/'db'; seeded(db).close()
    before=db.read_bytes()
    with ObserverServer(db,port=0) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            for _ in range(3):
                status,_,body=request(server,'/api/engines')
                assert status==200 and json.loads(body)==snapshot
        finally:
            server.shutdown();thread.join(timeout=5)
    assert calls==[1] and db.read_bytes()==before

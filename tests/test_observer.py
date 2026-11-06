from contextlib import contextmanager
from dataclasses import asdict
import http.client
import json
from pathlib import Path
import threading
import pytest
from test_store import seeded, TASK, RUN


@contextmanager
def running(db):
    from agent_office.observer import ObserverServer
    with ObserverServer(db, port=0) as server:
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

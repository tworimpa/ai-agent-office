from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import sqlite3
from uuid import uuid4

import pytest

from agent_office import team_runs
from test_away_http import POLICY
from agent_office.store import TaskStore
from agent_office.studio_ssh import _read_response, is_peer_route, RemoteUnavailable
from test_studio_http import project, request, studio
from test_studio_ssh import frame
from test_workspace_rooms import engine_snapshot, register


def test_wal_commits_during_a_read_snapshot_and_preserves_legacy_mode(tmp_path):
    legacy = tmp_path / "split-account.db"
    with TaskStore(legacy):
        pass
    for options in ({"wal_on_create": True}, {"readonly": True}, {"existing": True}):
        with TaskStore(legacy, **options) as store:
            assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    path = tmp_path / "private.db"
    with TaskStore(path, wal_on_create=True) as writer, TaskStore(path, readonly=True) as reader:
        assert writer.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        reader.connection.execute("BEGIN")
        reader.list_tasks()
        writer.connection.execute("PRAGMA busy_timeout=0")
        with writer._write():
            writer.connection.execute("CREATE TABLE new_table(id)")
        assert not reader.connection.execute("SELECT 1 FROM sqlite_master WHERE name='new_table'").fetchone()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.connection.execute("CREATE TABLE forbidden(id)")


def test_delegated_detail_finishes_while_rollback_writer_is_waiting(tmp_path, monkeypatch):
    folders = project(tmp_path)
    db = tmp_path / "managed.db"
    with studio(db) as server, TaskStore(db, existing=True) as writer:
        monkeypatch.setattr(server.runner, "_launch", lambda *_: None)
        rid = uuid4().hex
        receipt = server.runner.start(rid, uuid4().hex, folders[0].parent, ["."], "inspect",
                                      False, use_inbox=True, delegation=POLICY)
        original = server.runner._summary
        def queue_commit(row):
            # SQLite keeps the pending writer's transaction after SQLITE_BUSY.
            # New readers cannot start until this existing snapshot is released.
            writer.connection.execute("PRAGMA busy_timeout=0")
            writer.connection.execute("BEGIN IMMEDIATE")
            writer.connection.execute("UPDATE studio_runs SET diagnostic='concurrent' WHERE id=?", (rid,))
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.connection.execute("COMMIT")
            return original(row)
        monkeypatch.setattr(server.runner, "_summary", queue_commit)
        assert server.runner.detail(rid)["base_revision"] == receipt["base_revision"]
        writer.connection.execute("COMMIT")


def test_full_bundle_checks_every_peer_projection_and_readers_do_not_take_owner_lock(tmp_path, monkeypatch):
    folders = project(tmp_path)
    with studio(tmp_path / "journal.db") as server:
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("codex"))
        monkeypatch.setattr(server.runner, "_launch", lambda *_: None)
        workspace = register(server, folders)
        rid = uuid4().hex
        base = f"/rooms/{workspace}"
        status, receipt = request(server, f"/api{base}/assignments",
            {"id": rid, "message": "inspect", "allow_edits": False, "delegation": None})
        assert status == 202, receipt
        with ThreadPoolExecutor() as threads:
            with server.runner.lock:
                for read in (lambda: server.runner.detail(rid), server.runner.runs, lambda: server.rooms.snapshot(workspace)):
                    assert threads.submit(read).result(timeout=3)
        route = f"{base}/bundle/{rid}/full-usage"
        assert is_peer_route("GET", route) and not is_peer_route("POST", route)
        status, result = request(server, "/api" + route)
        assert status == 200, result
        assert set(result) == {"room", "run", "goal", "directions", "inbox", "briefing", "github", "usage"}
        def check():
            wire = frame(json.dumps(result).encode(), 200, server.journal_id)
            return _read_response(BytesIO(wire), server.journal_id, "GET", route, None)
        assert check() == (200, result)
        result["usage"]["workspace_id"] = uuid4().hex
        with pytest.raises(RemoteUnavailable):
            check()
        other = register(server, folders[:1])
        assert request(server, f"/api/rooms/{other}/bundle/{rid}/live")[0] == 404


@pytest.mark.parametrize("slots", ["1", "6", "0", "7", "", "2.5"])
def test_host_team_slots_are_bounded(slots, monkeypatch):
    monkeypatch.setenv("AGENT_OFFICE_TEAM_SLOTS", slots)
    if slots in ("1", "6"):
        assert team_runs.active_slots() == int(slots)
    else:
        with pytest.raises(ValueError, match="AGENT_OFFICE_TEAM_SLOTS"):
            team_runs.active_slots()

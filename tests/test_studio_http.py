"""Exercise the studio's public HTTP contract with two local stand-in CLI programs."""
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
import uuid

import pytest

from agent_office.studio_server import StudioServer


@contextmanager
def studio(db, **options):
    with StudioServer(db, port=0, **options) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def request(server, path, payload=None, **headers):
    address = server.server_address
    connection = HTTPConnection(*address, timeout=10)
    options = {}
    if payload is not None:
        options = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{address[1]}",
            "X-Agent-Office": "studio",
        }
    try:
        connection.request("POST" if payload is not None else "GET", path,
                           json.dumps(payload) if payload is not None else None,
                           {**options, **headers})
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if "application/json" in response.headers["Content-Type"] else raw
    finally:
        connection.close()


def workers(tmp_path, monkeypatch, *, edit_gate=None):
    bin_dir = tmp_path / "mock cli"
    bin_dir.mkdir()
    gate = ""
    if edit_gate is not None:
        gate = (
            f"gate = Path({str(edit_gate)!r})\n"
            "with gate.with_suffix('.calls').open('a') as calls: calls.write('edit\\n')\n"
            "while not gate.exists(): time.sleep(0.03)\n"
        )
    for engine in ("codex", "agy"):
        worker = bin_dir / f"{engine}.py"
        worker.write_text(
            "from pathlib import Path\nimport json, sys, time\n"
            "prompt = sys.stdin.read()\n"
            "root = next(p for p in (Path.cwd(), *Path.cwd().parents) if (p / '.git').exists())\n" +
            (gate + "assert 'contracts' in prompt and 'web' in prompt\n"
             "if 'WAIT_FOR_STOP' in prompt:\n"
             "    (root / 'started').touch()\n"
             "    time.sleep(90)\n"
             "(root / 'web' / 'value.txt').write_text((root / 'contracts' / 'wanted.txt').read_text())\n"
             "(root / 'contracts' / 'result.txt').write_text('contract applied')\n"
             "(root / 'web' / '신규.py').write_text('VERSION = 2', encoding='utf-8')\n"
             "(root / 'token.txt').write_text('private fixture', encoding='utf-8')\n"
             "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'fixture edit'}}))\n"
             if engine == "codex" else
             "print(json.dumps({'status':'SUCCESS','response':'VERDICT: APPROVED'}))\n"),
            encoding="utf-8",
        )
        if os.name == "nt":
            (bin_dir / f"{engine}.cmd").write_text(f'@"{sys.executable}" "%~dp0{engine}.py"\n')
        else:
            launcher = bin_dir / engine
            launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(worker))}\n")
            launcher.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])


def project(tmp_path):
    repo = tmp_path / "테스트 저장소"
    web, contracts = repo / "web", repo / "contracts"
    web.mkdir(parents=True)
    contracts.mkdir()
    (web / "value.txt").write_text("old")
    (contracts / "wanted.txt").write_text("new")
    (contracts / "test_contract.py").write_text(
        "from pathlib import Path\ndef test_contract():\n    assert Path(__file__).with_name('wanted.txt').read_text() == 'new'\n"
    )
    (web / "test_value.py").write_text(
        "from pathlib import Path\ndef test_value():\n    assert Path(__file__).with_name('value.txt').read_text() == 'new'\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    return web, contracts


def wait_for(server, run_id, states, *, api="/api"):
    end = time.monotonic() + 45
    while time.monotonic() < end:
        status, data = request(server, f"{api}/runs/{run_id}")
        assert status == 200, data
        run = data["run"]
        if run["state"] in states:
            return run
        assert run["state"] not in {"FAILED", "RECOVERY_REQUIRED", "WAITING_USER"}, run
        time.sleep(0.1)
    pytest.fail(f"Timed out polling {run_id}: {run}")


def test_studio_edit_review_reopen_and_stop(tmp_path, monkeypatch):
    workers(tmp_path, monkeypatch)
    web, contracts = project(tmp_path)
    db = tmp_path / "store" / "tasks.sqlite3"
    with studio(db) as server:
        status, data = request(server, "/api/workspaces", {"name": "상점", "paths": [str(web), str(contracts)]})
        assert status == 201, data
        ws_id = data["workspace"]["id"]
        run_id = uuid.uuid4().hex
        job = {"id": run_id, "workspace_id": ws_id, "task": "규격에 맞춰 수정", "indices": [0, 1], "allow_edits": True}
        status, data = request(server, "/api/runs", job)
        assert status == 202, data
        assert request(server, "/api/runs", job)[0] == 202
        run = wait_for(server, run_id, {"READY_FOR_HUMAN_REVIEW"})
        assert "fixture edit" in run["developer_output"] and "2 passed" in run["test_output"]
        assert "APPROVED" in run["review_output"] and "value.txt" in run["changes"]
        assert "+new" in run["diff"] and (web / "value.txt").read_text() == "old"
        assert "신규.py" in run["changes"] and "+VERSION = 2" in run["diff"]
        assert "private fixture" not in run["diff"] and "token.txt" in run["changes"]
        contracts.rename(contracts.with_name("relocated"))
        assert request(server, "/api/runs", job)[1]["run"]["id"] == run_id
        contracts.with_name("relocated").rename(contracts)
        assert len(request(server, "/api/runs")[1]["runs"]) == 1
        assert request(server, "/api/runs", {**job, "task": "other"})[0] == 409
    with studio(db) as server:
        assert request(server, "/api/workspaces")[1]["workspaces"][0]["id"] == ws_id
        assert request(server, f"/?run={run_id}")[0] == 200
        assert request(server, f"/api/runs/{run_id}")[1]["run"]["diff"] == run["diff"]
        second = uuid.uuid4().hex
        assert request(server, "/api/runs", {**job, "id": second, "task": "WAIT_FOR_STOP"})[0] == 202
        wait_for(server, second, {"RUNNING"})
        stop = request(server, f"/api/runs/{second}/stop", {})
        assert stop[0] == 200 and stop[1]["run"]["state"] in {"STOP_REQUESTED", "CANCELED"}
        assert wait_for(server, second, {"CANCELED"})["state"] == "CANCELED"


def test_studio_accepts_only_local_explicit_json_commands(tmp_path):
    with studio(tmp_path / "tasks.db") as server:
        with pytest.raises(OSError):
            StudioServer(tmp_path / "other.db", server.server_port)
        for headers in ({"Origin": "null"}, {"Origin": "http://unrelated.test"},
                        {"Host": "unrelated.test"}, {"X-Agent-Office": ""},
                        {"Sec-Fetch-Site": "cross-site"}):
            assert request(server, "/api/workspaces", {}, **headers)[0] == 403
        assert request(server, "/api/workspaces", {}, **{"Content-Type": "text/plain"})[0] == 400
        assert request(server, "/api/runs", {"task": "missing fields"})[0] == 400
        assert request(server, "/api/runs/" + uuid.uuid4().hex)[0] == 404
        for path in ("/", "/studio.css", "/studio.js"):
            status, body = request(server, path)
            assert status == 200 and body
        assert request(server, "/?run=bad")[0] == 400
        assert request(server, "/api/workspaces")[1] == {"workspaces": []}

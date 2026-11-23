from contextlib import nullcontext
import json
import socket
import subprocess
import sys
import time
import uuid

import pytest

from agent_office import engine_evidence, engine_updates
from agent_office.batch_dialogue import _turn
from agent_office.provider_telemetry import acp_usage, batch_receipt, claude_stream
from test_engine_evidence import installed
from test_studio_http import project, request, studio, wait_for
from test_studio_runner import install_fake


def test_bounded_batch_stderr_only_publishes_recognized_cause(tmp_path):
    code = "import sys; sys.stderr.write('very-private'*20000+'\\nAPI_TOKEN=private model_not_found\\n'); sys.exit(2)"
    result = _turn("claude", [sys.executable, "-c", code], "prompt", tmp_path, None, time.monotonic()+8, nullcontext())
    assert result.returncode == 2 and result.message == "claude exited 2 (model_not_found)"


def test_batch_timeout_releases_descendants_socket(tmp_path):
    child = ("import socket,time; from pathlib import Path; s=socket.socket(); s.bind(('127.0.0.1',0));"
             "s.listen(); Path('port').write_text(str(s.getsockname()[1])); time.sleep(30)")
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]);time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        _turn("claude", [sys.executable, "-c", parent, child], "prompt", tmp_path, None,
              time.monotonic()+3, nullcontext())
    port = int((tmp_path / "port").read_text())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_cline_new_version_requires_same_runtime_and_capability(tmp_path, installed, monkeypatch):
    executable, version = installed
    db = tmp_path / "journal"
    version[0] = "3.0.75"
    monkeypatch.setattr(engine_evidence, "_run_fixture", lambda e,c,v: (v == version[0], "fixture_passed"))
    assert engine_evidence.verify_engine(db, "cline", "read")["status"] == "passed"
    assert engine_evidence.cline_verified(db, str(executable), version[0], "read")
    assert not engine_evidence.cline_verified(db, str(executable), version[0], "edit")
    assert not engine_evidence.cline_verified(db, str(executable), "3.1.0", "read")
    executable.write_bytes(b"replacement")
    assert not engine_evidence.cline_verified(db, str(executable), version[0], "read")


def test_non_codex_receipts_keep_context_cost_and_totals_distinct():
    record = {"type":"result", "total_cost_usd":0.001,
              "usage":{"input_tokens":3,"output_tokens":7,"cache_read_input_tokens":20,"cache_creation_input_tokens":5}}
    assert batch_receipt("claude", json.dumps(record)) == [
        {"kind":"usage","total_tokens":35,"input_tokens":3,"output_tokens":7,"cached_input_tokens":20,"total_cost_usd":0.001}]
    agy = json.dumps({"event":"result","result":{"status":"SUCCESS","usage":{"total_tokens":20,"thinking_tokens":5}}})
    assert batch_receipt("antigravity", agy) == [{"kind":"usage","total_tokens":20,"reasoning_output_tokens":5}]
    assert batch_receipt("antigravity", agy + "\n" + agy) == []
    assert acp_usage({"used":27,"size":1024}) is None
    assert acp_usage({"totalTokens":100,"outputTokens":20}) == {"kind":"usage","total_tokens":100,"output_tokens":20}
    assert batch_receipt("claude", '{"type":"result","is_error":true,"api_error_status":429}') == []
    rows = [{"type":"rate_limit_event", "session_id":"one", "rate_limit_info":{
        "status":"rejected", "rateLimitType":"five_hour", "utilization":1, "resetsAt":1890000000}},
        {"type":"result", "session_id":"one"}]
    _, limits = claude_stream("\n".join(map(json.dumps, rows)))
    assert limits == [{"kind":"rate_limit","limit_id":"five_hour","ordinary_usage_allowed":False,
                       "windows":[{"name":"primary","used_percent":100,"resets_at":1890000000}]}]


def test_live_run_progress_replays_only_new_confirmed_activity(tmp_path, monkeypatch):
    install_fake(tmp_path, monkeypatch, "import json,sys\nsys.stdin.read()\n"
                 "print(json.dumps({'type':'thread.started','thread_id':'test'}))\n"
                 "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))\n")
    folder, _ = project(tmp_path)
    run_id = uuid.uuid4().hex
    db = tmp_path / "db"
    with studio(db) as server:
        _, value = request(server, "/api/workspaces", {"name":"work","paths":[str(folder)]})
        job = {"id":run_id,"workspace_id":value["workspace"]["id"],"indices":[0],"task":"read","allow_edits":False}
        assert request(server, "/api/runs", job)[0] == 202
        run = wait_for(server, run_id, {"PLAN_ONLY"})
        assert run["last_progress"]["engine"] == "codex" and run["last_progress"]["at"]
        path = f"/api/runs/{run_id}/stream?office_journal={server.journal_id}"
        status, raw = request(server, path)
        assert status == 200 and b"id: " in raw and b"event: finished" in raw
        _, again = request(server, path, **{"Last-Event-ID":str(run["last_progress"]["id"])})
        assert b"id: " not in again and b"event: finished" in again
        assert request(server, path, **{"Last-Event-ID":"-1"})[0] == 400


def test_updates_require_idle_host_and_exact_versions(tmp_path, monkeypatch):
    db = tmp_path / "db"
    monkeypatch.setattr(engine_updates, "check", lambda e: dict(engine=e,installed="1.0.0",latest="2.0.0",update_available=True))
    with studio(db) as server:
        path = "/api/engines/cline/"
        assert request(server, path + "check", {})[1]["latest"] == "2.0.0"
        with engine_updates.host_lease():
            assert request(server, path+"update", {"installed":"1.0.0","latest":"2.0.0"})[0] == 400
        assert request(server, path+"update", {"installed":"0.1.0","latest":"2.0.0"})[0] == 400
        assert request(server, path+"update", {"argv":["arbitrary"]})[0] == 400
        assert request(server, "/api/peer/"+server.journal_id+"/engines/cline/update", {})[0] == 404
        monkeypatch.setattr(engine_updates, "resolve_executable", lambda _: sys.executable)
        monkeypatch.setattr(engine_updates, "update_command", lambda *a: [sys.executable,"-c","pass"])
        monkeypatch.setattr(engine_updates, "probe_version", lambda *a: ("2.0.0","ok"))
        assert request(server, path+"update", {"installed":"1.0.0","latest":"2.0.0"}) == (
            200, {"engine":"cline","installed":"2.0.0"})

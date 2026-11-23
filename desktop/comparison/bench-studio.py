"""A disposable Studio with seeded history, one real supervised mock worker and a renderer observer."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from uuid import uuid4

from agent_office import goal_verification, workspace_briefing
from agent_office.engine_status import metadata_entry
from agent_office.engines import SUPPORTED_ENGINES
from agent_office.store import TaskStore, encode, now
from agent_office.studio_server import StudioHandler, StudioServer


HERE = Path(__file__).resolve().parent


def metadata():
    rows = []
    for name in SUPPORTED_ENGINES:
        available = name in {"codex", "antigravity"}
        row = metadata_entry(name, name if available else None)
        row.update(probe_status="ok" if available else "missing", headless_support=available,
                   capabilities={key: {"status": "not_checked"} for key in ("read", "edit", "review")})
        rows.append(row)
    return {"platform": sys.platform, "engines": rows}


def install_mock(root):
    mock = HERE.parent / "fixtures/goal_worker.py"
    trace = root / "provider-trace"
    trace.mkdir()
    for name in ("codex", "agy"):
        if os.name == "nt":
            (root / (name + ".cmd")).write_text(
                f'@"{sys.executable}" "{mock}" {name} --fixture-trace "{trace}" %*\n', encoding="utf-8")
        else:
            path = root / name
            command = shlex.join([sys.executable, str(mock), name, "--fixture-trace", str(trace)])
            path.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
            path.chmod(0o700)
    os.environ["PATH"] = str(root) + os.pathsep + os.environ["PATH"]
    os.environ["AGENT_OFFICE_GITHUB_CONFIG"] = str(root / "no-connections.json")


def seed(server, root):
    repo = root / "project"
    repo.mkdir()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, creationflags=flags)
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (repo / "settings.py").write_text('import json\n\ndef restore(path):\n    return "light"\n')
    (repo / "tests").mkdir()
    (repo / "tests/test_settings.py").write_text(
        'from settings import restore\n\ndef test_saved(tmp_path):\n'
        '    path = tmp_path / "settings.json"\n    path.write_text(\'{"theme":"dark"}\')\n'
        '    assert restore(path) == "dark"\n')
    subprocess.run(["git", "add", "."], cwd=repo, check=True, creationflags=flags)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "benchmark"], cwd=repo, check=True, creationflags=flags)
    workspace = server.catalog.create("성능 비교", [str(repo)])["id"]
    candidate, active = uuid4().hex, uuid4().hex

    def assign(run_id, message):
        server.rooms.assign(workspace, dict(id=run_id, message=message, allow_edits=True, delegation=False,
                                            goal=True, goal_criteria=["설정 복원"], test_files=["tests/test_settings.py"]))

    # Hold the normal scheduler while constructing historical records so it can never
    # send any synthesized item to a provider. The tenth run uses the ordinary scheduler.
    with server.runner.lock, TaskStore(server.db_path, existing=True) as store:
        db = store.connection
        goal_verification.init_schema(db)
        for i in range(9):
            rid = candidate if i == 0 else uuid4().hex
            assign(rid, f"설정 복원 검증 {i + 1}")
            db.execute("DELETE FROM studio_goal_queue WHERE run_id=?", (rid,))
            server.runner.queued_goals.discard(rid)
            server.runner.goal_prompts.pop(rid, None)
            if rid != candidate:
                store.record_transition(rid, operation_key="fixture-end", event_type="RUN_FINISHED",
                                        state="CANCELED", outcome="CANCELED")
                continue
            for state in ("RUNNING", "TESTING", "REVIEWING"):
                store.record_transition(rid, operation_key=state, event_type="STAGE_STARTED", state=state, attempts=1)
            store.record_transition(rid, operation_key="fixture-end", event_type="RUN_FINISHED",
                                    state="READY_FOR_HUMAN_REVIEW", outcome="APPROVED")
            patch = "diff --git a/settings.py b/settings.py\n--- a/settings.py\n+++ b/settings.py\n@@ -1 +1 @@\n-RESTORE = False\n+RESTORE = True\n"
            digest = hashlib.sha256(patch.encode()).hexdigest()
            record = dict(attempt=1, created_at=now(), digest=digest, score=1000000, eligible=True,
                          status="passed", change="settings.py", files=["settings.py"])
            db.execute("INSERT INTO studio_goal_candidates VALUES(?,?,?,?)", (rid, 1, encode(record), patch))
            db.execute("INSERT INTO studio_goal_reviews VALUES(?,1,'review',?,'approved',?,?)",
                       (rid, digest, "저장 설정 복원", now()))
        assign(active, "실행 중인 목표의 설정 동작 검토")

    for _ in range(1200):
        with server.runner.lock:
            child = server.runner.children.get(active)
            question = next((row["id"] for row in server.runner.inbox.pending()
                             if row["run_id"] == active and row["kind"] == "question"), None)
            if child and child.poll() is None and question:
                traces = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "provider-trace").glob("*.json")]
                live = [p["pid"] for p in traces if p["engine"] == "codex" and "ended" not in p]
                if len(live) != 1:
                    raise RuntimeError("Expected one live mock Codex holding the question")
                with TaskStore(server.db_path, readonly=True) as store:
                    board = workspace_briefing.read(store.connection, workspace)
                    assert len(board["work"]) == 10 and len(board["candidates"]) == 1
                    assert any(row.get("request_id") == question for row in board["decisions"])
                return dict(workspace=workspace, active=active, candidate=candidate, question=question,
                            worker=child.pid, cli=live[0])
            state = server.runner.detail(active)
            if state["state"] not in {"QUEUED", "RUNNING", "TESTING", "REVIEWING"}:
                raise RuntimeError("Mock goal stopped before the expected question: " + str(state))
        time.sleep(0.1)
    raise RuntimeError("The active fixture did not start its supervised mock CLI: " +
                       str(server.runner.detail(active).get("diagnostic", "")))


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(exist_ok=False)
    install_mock(root)
    probe = (HERE / "bench-page.js").read_bytes()

    class Handler(StudioHandler):
        def _reply(self, status, body, mime="application/json"):
            if self.path == "/workspace-office.js" and status == 200:
                config = (root / "trial.json").read_bytes()
                body += b"\n" + probe + b"\nobserveOffice(" + config + b")\n"
            super()._reply(status, body, mime)

        def _local_api(self, path, data):
            if path == "/api/benchmark-event" and data is not None:
                trial = json.loads((root / "trial.json").read_text(encoding="utf-8"))
                if data.get("nonce") != trial["nonce"] or data.get("event") not in {"viewport", "ready", "done", "error"}:
                    return self._error(400, "BAD_REQUEST")
                data["observed"] = time.perf_counter()
                with (root / (trial["nonce"] + ".jsonl")).open("a", encoding="utf-8") as output:
                    print(json.dumps(data), file=output)
                if data["event"] == "viewport":
                    ack = root / (trial["nonce"] + ".sized")
                    for _ in range(500):
                        if ack.exists():
                            break
                        time.sleep(.01)
                    else:
                        return self._error(500, "VIEWPORT_TIMEOUT")
                return self._reply(200, {"ok": True})
            return super()._local_api(path, data)

    with StudioServer(root / "benchmark.sqlite3", 0) as server:
        server.RequestHandlerClass = Handler
        server.engines.snapshot = metadata
        setup = seed(server, root) if sys.argv[2] == "loaded" else {}
        setup.update(origin=f"http://127.0.0.1:{server.server_port}", journalId=server.journal_id, studio=os.getpid())
        print(json.dumps(setup), flush=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            sys.stdin.buffer.read(1)
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()

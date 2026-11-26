from contextlib import nullcontext
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from agent_office.engines import SUPPORTED_ENGINES
from agent_office.engine_status import LABELS
from agent_office.studio_ssh import RemoteHost, RemoteUnavailable, _read_response
from test_codex_inbox import install_mock_codex
from test_inbox_http import wait_request
from test_remote_studio import LoopbackLink, assert_unavailable
from test_studio_http import project, request, studio, wait_for, workers
from test_studio_ssh import frame


def engine_snapshot(*available):
    return {"platform": "fixture", "engines": [
        {"engine": engine, "label": LABELS[engine], "installed": engine in available,
         "headless_support": engine in available, "version": None, "auth_status": "unknown",
         "probe_status": "ok" if engine in available else "missing",
         "capabilities": {key: {"status": "not_checked", "checked_at": None} for key in ("read", "edit", "review")}}
        for engine in SUPPORTED_ENGINES]}


def register(server, paths, name="Workspace", api="/api"):
    status, data = request(server, f"{api}/workspaces", {"name": name, "paths": [str(path) for path in paths]})
    assert status == 201, data
    return data["workspace"]["id"]


def test_room_assignment_preserves_registered_git_trust(tmp_path, monkeypatch):
    web, contracts = project(tmp_path)
    repo = web.parent
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    trust = tmp_path / "service-gitconfig"
    trust.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(trust))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    run = subprocess.run

    def with_different_owner(argv, **options):
        # Keep the test switch out of the caller's environment. Git in the
        # production service compares the real checkout owner to its own account.
        if Path(argv[0]).stem.casefold() == "git":
            env = dict(options.get("env") or os.environ)
            env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
            options["env"] = env
        return run(argv, **options)

    monkeypatch.setattr(subprocess, "run", with_different_owner)
    with studio(tmp_path / "tasks.db") as local:
        monkeypatch.setattr(local.runner, "_launch", lambda *_args: None)
        monkeypatch.setattr(local.engines, "snapshot", lambda: engine_snapshot("codex"))
        workspace = register(local, (web, contracts))
        route = f"/api/rooms/{workspace}/assignments"
        task = {"id": uuid4().hex, "message": "파일 수정 없이 검토", "allow_edits": False, "delegation": None}
        assert request(local, route, task)[0] == 400
        trust.write_text(f'[safe]\n\tdirectory = "{repo.as_posix()}"\n', encoding="utf-8")
        status, receipt = request(local, route, task)
        assert status == 202, receipt
        assert receipt["run"]["base_revision"] == revision
        assert receipt["run"]["allow_edits"] is False


def room_choice_fixture(tmp_path, monkeypatch):
    workers(tmp_path, monkeypatch)
    install_mock_codex(tmp_path / "mock cli", monkeypatch, "room-choice")
    name = tmp_path / "mock cli" / "agy"
    helper = Path(__file__).parent / "helpers" / "away_roles.py"
    if os.name == "nt":
        name.with_suffix(".cmd").write_text(f'@"{sys.executable}" "{helper}" agy %*\n')
    else:
        name.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} agy "$@"\n')
        name.chmod(0o700)
    calls = tmp_path / "coordinator-calls"
    monkeypatch.setenv("OFFICE_AWAY_CALLS", str(calls))
    web, contracts = project(tmp_path)
    (web / "first.py").write_text("def read_saved_result():\n    return 'existing'\n", encoding="utf-8")
    (web / "second.py").write_text("def clear_saved_result():\n    return None\n", encoding="utf-8")
    subprocess.run(["git", "add", "web/first.py", "web/second.py"], cwd=web.parent, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=fixture@example.invalid", "commit", "-qm",
                    "existing code choices"], cwd=web.parent, check=True)
    return web, contracts, calls


def test_room_settings_and_assignment_snapshot_survive_retry(tmp_path, monkeypatch):
    web, contracts = project(tmp_path)
    launched = []
    with studio(tmp_path / "tasks.db") as local:
        monkeypatch.setattr(local.runner, "_launch", lambda _store, run, prompt: launched.append((run, prompt)))
        engines = engine_snapshot("gemini", "antigravity")
        next(row for row in engines["engines"] if row["engine"] == "gemini")["capabilities"]["read"] = {
            "status": "passed", "checked_at": "2026-11-20T00:00:00+00:00"}
        monkeypatch.setattr(local.engines, "snapshot", lambda: engines)
        workspace = register(local, (web, contracts))
        other = register(local, (web,), "다른 방")
        room = f"/api/rooms/{workspace}"
        status, data = request(local, room)
        assert status == 200 and data["settings"]["developer"]["engine"] == "codex"
        _, item = request(local, "/api/skills", {"name": "검토 스킬", "instructions": "Check related tests."})
        settings = data["settings"]
        settings["delegation"] = True
        settings["developer"].update(name="하나", skills=[item["skill"]["id"]])
        assert request(local, room, settings) == (200, {"settings": settings})
        assignment = {"id": uuid4().hex, "message": "파일 변경 없이 검토해줘.",
                      "allow_edits": False, "delegation": False}
        status, data = request(local, room + "/assignments", assignment)
        assert status == 202, data
        run = data["run"]
        assert run["assignment"]["roles"]["developer"] == {**settings["developer"], "engine": "gemini"}
        assert run["assignment"]["delegation"] is False and run["delegation"] is None
        assert run["folders"] == ["web", "contracts"] and run["allow_edits"] is False
        assert run["task"] == assignment["message"]
        assert request(local, f"/api/rooms/{other}")[1]["runs"] == []
        assert request(local, room)[1]["runs"][0]["lines"]["developer"] == "작업 시작을 기다리고 있습니다."
        settings["developer"]["name"] = "둘"
        assert request(local, room, settings)[0] == 200
        monkeypatch.setattr(local.engines, "snapshot", lambda: engine_snapshot())
        assert request(local, room + "/assignments", assignment) == (202, {"run": run})
        assert request(local, room + "/assignments", {**assignment, "delegation": True})[0] == 409
        assert launched == [(assignment["id"], assignment["message"])]
    with studio(tmp_path / "tasks.db") as reopened:
        monkeypatch.setattr(reopened.engines, "snapshot", engine_snapshot)
        status, data = request(reopened, room)
        assert status == 200 and data["settings"] == settings
        assert data["runs"][0]["assignment"] == run["assignment"]


def test_enabled_delegation_is_pinned_even_without_a_factual_rule(tmp_path, monkeypatch):
    from agent_office.away_ledger import get_choices

    web, contracts = project(tmp_path)
    with studio(tmp_path / "tasks.db") as local:
        monkeypatch.setattr(local.runner, "_launch", lambda *_args: None)
        monkeypatch.setattr(local.engines, "snapshot", lambda: engine_snapshot("codex"))
        workspace = register(local, (web, contracts))
        room = f"/api/rooms/{workspace}"
        settings = request(local, room)[1]["settings"]
        settings["delegation"] = True
        assert request(local, room, settings)[0] == 200
        jobs = [{"id": uuid4().hex, "message": "기존 코드 검토", "allow_edits": False, "delegation": override}
                for override in (None, False)]
        for job, enabled in zip(jobs, [True, False], strict=True):
            status, body = request(local, room + "/assignments", job)
            assert status == 202, body
            run = body["run"]
            assert run["assignment"]["delegation"] is enabled
            assert run["delegation"] is None and run["delegation_active"] is enabled
            assert get_choices(local.db_path, job["id"]) is enabled
        run = jobs[0]["id"]
        assert request(local, f"/api/runs/{run}/delegation/stop", {}) == (200, {"run_id": run, "active": False})
        assert get_choices(local.db_path, run) is False


@pytest.mark.parametrize("case", ["delegated", "manual", "product"])
def test_workspace_coordinator_records_evidence_and_keeps_product_decisions_human(tmp_path, monkeypatch, case):
    web, contracts, calls = room_choice_fixture(tmp_path, monkeypatch)
    if case == "product":
        monkeypatch.setenv("OFFICE_FIXTURE_CODEX", "room-product")
    elif case == "delegated":
        monkeypatch.setenv("OFFICE_FIXTURE_ROOM_ANSWER", "web/first.py:1")
    run_id = uuid4().hex
    with studio(tmp_path / "decision.db") as local:
        monkeypatch.setattr(local.engines, "snapshot", lambda: engine_snapshot("codex", "antigravity"))
        workspace = register(local, (web, contracts))
        room = f"/api/rooms/{workspace}"
        settings = request(local, room)[1]["settings"]
        settings["delegation"] = True
        assert request(local, room, settings)[0] == 200
        status, body = request(local, room + "/assignments", {"id": run_id, "message": "원래 작업 · 기존 결과 표시를 검토해줘",
                                                             "allow_edits": False, "delegation": False if case == "manual" else None})
        assert status == 202, body
        path = f"/api/runs/{run_id}/inbox"
        if case != "delegated":
            pending = wait_request(local, "/api", run_id, "question" if case == "manual" else "product_decision")
            decision = path + f"/{pending['id']}/decision"
            assert request(local, decision, {"action": "approve"})[0] == 400
            assert request(local, decision, {"action": "answer", "answer": "둘째 방식"})[0] == 200
        result = wait_for(local, run_id, {"PLAN_ONLY"})
        assert "작업 완료" in result["developer_output"]
        row, = request(local, path)[1]["requests"]
        assert row["status"] == "delivered"
        if case == "delegated":
            assert row["source"] == "delegate"
            evidence = row["delegation_details"]["evidence"]
            assert evidence == {"path": "web/first.py", "line": 1, "quote": "def read_saved_result():",
                                "revision": result["base_revision"], "purpose": "diagnosis", "option": 1}
            assert evidence == request(local, "/api/return-report")[1]["delegated"][0]["evidence"]
            assert [json.loads(line)[0] for line in calls.read_text().splitlines()] == ["AWAY_SPECIALIST", "AWAY_VERIFIER"]
        else:
            assert row["source"] == "operator" and row["answer"] == "둘째 방식"
            assert not calls.exists()


def test_remote_assignment_lost_receipt_resolves_on_same_host(tmp_path, monkeypatch):
    web, contracts = project(tmp_path / "remote")
    with studio(tmp_path / "peer.db", accept_remote=True) as peer:
        monkeypatch.setattr(peer.engines, "snapshot", lambda: engine_snapshot("codex", "antigravity"))
        launched = []
        monkeypatch.setattr(peer.runner, "_launch", lambda _store, run, prompt: launched.append(run))
        host = RemoteHost("fixture", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)
        lose_receipt = True

        def forward(target, method, route, body):
            nonlocal lose_receipt
            response = link(target, method, route, body)
            if lose_receipt and route.endswith("/assignments") and response[0] == 202:
                lose_receipt = False
                link.online = False
                raise RemoteUnavailable()
            return response

        monkeypatch.setattr("agent_office.studio_server.remote_request", forward)
        with studio(tmp_path / "local.db", remotes=(host,)) as local:
            api = "/api/hosts/" + host.journal_id
            workspace = register(local, (web, contracts), api=api)
            room = f"{api}/rooms/{workspace}"
            assignment = {"id": uuid4().hex, "message": "원격 코드 검토", "allow_edits": False, "delegation": None}
            assert_unavailable(request(local, room + "/assignments", assignment))
            assert_unavailable(request(local, room))
            link.online = True
            assert [row["id"] for row in request(local, room)[1]["runs"]] == [assignment["id"]]
            status, data = request(local, room + "/assignments", assignment)
            assert status == 202 and data["run"]["id"] == assignment["id"]
            assert request(local, f"/api/runs/{assignment['id']}")[0] == 404
            assert launched == [assignment["id"]]


def test_mixed_version_peer_receipts_preserve_existing_workspace_protocol(tmp_path, monkeypatch):
    from agent_office.studio_ssh import ROLE_BOT_RECEIPT_HEADER, _request_bytes

    web, _ = project(tmp_path)
    with studio(tmp_path / "peer.db", accept_remote=True) as peer:
        monkeypatch.setattr(peer.runner, "_launch", lambda *_: None)
        monkeypatch.setattr(peer.engines, "snapshot", lambda: engine_snapshot("codex"))
        workspace = register(peer, (web,))
        room = f"/rooms/{workspace}"
        prefix = f"/api/peer/{peer.journal_id}"
        host = RemoteHost("fixture", peer.server_port, peer.journal_id)
        header = {ROLE_BOT_RECEIPT_HEADER: "1"}
        assert f"{ROLE_BOT_RECEIPT_HEADER}: 1\r\n".encode() in _request_bytes(host, "GET", room, None)

        bot = {"id": "release-dev", "role": "developer", "name": "Release developer",
               "instructions": "Summarize public changes."}
        assert request(peer, "/api" + room + "/bots", bot)[0] == 200
        assert request(peer, "/api" + room + "/bots/assignment", {"id": bot["id"], "role": bot["role"]})[0] == 200
        run = uuid4().hex
        task = {"id": run, "message": "Plan the release.", "allow_edits": False, "delegation": None}
        status, legacy = request(peer, prefix + room + "/assignments", task)
        assert status == 202 and "bots" not in legacy["run"]["assignment"]
        status, modern = request(peer, prefix + room + "/assignments", task, **header)
        assert status == 202 and modern["run"]["assignment"]["bots"]["developer"]["id"] == bot["id"]
        assert request(peer, "/api/runs/" + run)[1]["run"]["assignment"] == modern["run"]["assignment"]

        for headers, has_bots in [({}, False), (header, True)]:
            _, page = request(peer, prefix + room, **headers)
            _, detail = request(peer, prefix + "/runs/" + run, **headers)
            _, bundle = request(peer, prefix + room + f"/bundle/{run}/briefing", **headers)
            for row in (page["runs"][0], detail["run"], bundle["room"]["runs"][0], bundle["run"]["run"]):
                assert ("bots" in row["assignment"]) is has_bots
                assert row["assignment"]["roles"]["developer"]["name"] == bot["name"]


def test_room_history_pages_keep_workspace_scope(tmp_path, monkeypatch):
    web, contracts = project(tmp_path)
    monkeypatch.setattr("agent_office.workspace_rooms.ROOM_LIMIT", 2)
    with studio(tmp_path / "tasks.db") as local:
        monkeypatch.setattr(local.engines, "snapshot", lambda: engine_snapshot("codex"))
        monkeypatch.setattr(local.runner, "_launch", lambda *_args: None)
        workspace = register(local, (web, contracts))
        other = register(local, (web,), "다른 작업실")
        room = f"/api/rooms/{workspace}"
        ids = [uuid4().hex for _ in range(5)]
        for run_id in ids:
            assert request(local, room + "/assignments", {"id": run_id, "message": "이전 작업",
                           "allow_edits": False, "delegation": None})[0] == 202
        snapshot = request(local, room)[1]
        all_ids = [row["id"] for row in snapshot["runs"]]
        cursor = snapshot["next_cursor"]
        assert request(local, f"/api/rooms/{other}/history/{cursor}")[0] == 404
        while cursor:
            status, page = request(local, f"{room}/history/{cursor}")
            assert status == 200, page
            all_ids.extend(row["id"] for row in page["runs"])
            cursor = page["next_cursor"]
        assert all_ids == list(reversed(ids))


def test_room_records_codex_start_failure_without_provider_message(tmp_path, monkeypatch):
    from contextlib import closing

    workers(tmp_path, monkeypatch)
    install_mock_codex(tmp_path / "mock cli", monkeypatch, "rpc-rejected")
    web, contracts = project(tmp_path)
    db = tmp_path / "tasks.db"
    with studio(db) as local:
        monkeypatch.setattr(local.engines, "snapshot", lambda: engine_snapshot("codex", "antigravity"))
        workspace = register(local, (web, contracts))
        run_id = uuid4().hex
        status, data = request(local, f"/api/rooms/{workspace}/assignments",
                               {"id": run_id, "message": "원래 작업", "allow_edits": True, "delegation": None})
        assert status == 202, data
        run = wait_for(local, run_id, {"FAILED"})
        expected = "Codex app-server [thread/start] protocol: RPC rejected (code=-32602)."
        assert run["developer_output"] == expected
        assert request(local, f"/api/runs/{run_id}/inbox") == (200, {"requests": []})
    with closing(sqlite3.connect(db)) as journal:
        saved = journal.execute("SELECT developer_output FROM studio_runs WHERE id=?", (run_id,)).fetchone()
        assert saved == (expected,)


def test_room_rejects_invalid_test_selection_before_launch(tmp_path, monkeypatch):
    web, contracts = project(tmp_path)
    with studio(tmp_path / "tasks.db") as local:
        monkeypatch.setattr(local.engines, "snapshot", lambda: engine_snapshot("codex"))
        monkeypatch.setattr(local.runner, "_launch", lambda *_args: pytest.fail("Unexpected execution"))
        workspace = register(local, (web, contracts))
        route = f"/api/rooms/{workspace}/assignments"
        for files in ([], ["../test_outside.py"], ["-k"], ["web"], ["C:/test_file.py"],
                      ["web/test_missing.py"], ["web/test_value.py", "web/test_value.py"]):
            job = {"id": uuid4().hex, "message": "원래 작업", "allow_edits": True,
                   "delegation": None, "test_files": files}
            assert request(local, route, job)[0] == 400
        job = {"id": uuid4().hex, "message": "원래 작업", "allow_edits": False,
               "delegation": None, "test_files": ["web/test_value.py"]}
        assert request(local, route, job)[0] == 400
        monkeypatch.setattr("agent_office.workspace_rooms.find_spec", lambda name: None)
        status, data = request(local, route, {**job, "allow_edits": True})
        assert status == 400 and "pytest" in data["error"]["message"]
        assert request(local, "/api/runs") == (200, {"runs": []})


@pytest.mark.parametrize("remote", [False, True])
def test_room_assignment_continues_through_direction_and_results(tmp_path, monkeypatch, remote):
    workers(tmp_path, monkeypatch)
    install_mock_codex(tmp_path / "mock cli", monkeypatch, "direction")
    web, contracts = project(tmp_path)
    repo = web.parent
    (web / "test_value.py").write_text(
        "from pathlib import Path\ndef test_patch():\n"
        "    assert (Path(__file__).parents[1] / 'answer.txt').read_text() == 'fixture'\n", encoding="utf-8")
    if remote:
        (contracts / "test_contract.py").write_text("def test_outside_scope():\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qam", "test assigned result"], check=True, cwd=repo)

    with studio(tmp_path / "peer.db", accept_remote=True) as peer:
        monkeypatch.setattr(peer.engines, "snapshot", lambda: engine_snapshot("codex", "antigravity"))
        host = RemoteHost("workspace-peer", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)

        def forward(target, method, route, body):
            status, data = link(target, method, route, body)
            return _read_response(BytesIO(frame(json.dumps(data).encode(), status, host.journal_id)),
                                  host.journal_id, method, route, body)

        monkeypatch.setattr("agent_office.studio_server.remote_request", forward)
        api = "/api/hosts/" + host.journal_id if remote else "/api"
        with (studio(tmp_path / "local.db", remotes=(host,)) if remote else nullcontext(peer)) as local:
            workspace = register(local, (web, contracts), api=api)
            unrelated = register(local, (web,), "별도 작업실", api=api)
            room = f"{api}/rooms/{workspace}"
            run_id = uuid4().hex
            job = {"id": run_id, "message": "원래 작업", "allow_edits": True, "delegation": None}
            if remote:
                job["test_files"] = ["web/test_value.py"]
            status, body = request(local, room + "/assignments", job)
            assert status == 202, body
            if remote:
                assert body["run"]["assignment"]["test_files"] == job["test_files"]
                assert request(local, room + "/assignments", {**job, "test_files": ["contracts/test_contract.py"]})[0] == 409
            path = f"{api}/runs/{run_id}"
            question = wait_request(local, api, run_id, "question")
            assert request(local, room + "/inbox") == (200, {"requests": [question]})
            assert request(local, f"{api}/rooms/{unrelated}/inbox") == (200, {"requests": []})
            row = request(local, room)[1]["runs"][0]
            assert row["pending_count"] == 1 and row["lines"]["developer"] == "사용자 응답을 기다리고 있습니다."
            direction = {"id": uuid4().hex, "role": "developer", "attempt": row["attempts"],
                         "message": '중간 지시 & "원문"'}
            status, data = request(local, path + "/directions", direction)
            assert status == 202 and data["direction"]["delivery"] == "queued", data
            assert request(local, path + "/directions", direction) == (status, data)
            if remote:
                link.online = False
                assert_unavailable(request(local, path))
                link.online = True
            assert request(local, path + f"/inbox/{question['id']}/decision",
                           {"action": "answer", "answer": "둘째 방식"})[0] == 200
            permission = wait_request(local, api, run_id, "permission")
            assert request(local, path + f"/inbox/{permission['id']}/decision", {"action": "approve"})[0] == 200
            run = wait_for(local, run_id, {"READY_FOR_HUMAN_REVIEW"}, api=api)
            assert "지시 반영" in run["developer_output"]
            assert ("1 passed" if remote else "2 passed") in run["test_output"]
            if remote:
                assert run["assignment"]["test_files"] == job["test_files"]
            assert "APPROVED" in run["review_output"] and "answer.txt" in run["changes"]
            assert not (repo / "answer.txt").exists()
            _, decisions = request(local, path + "/inbox")
            assert [(entry["decision"], entry["status"]) for entry in decisions["requests"]] == [
                ("answer", "delivered"), ("approve", "delivered")]
            _, data = request(local, path + "/directions")
            saved, = data["directions"]
            assert saved["id"] == direction["id"] and saved["delivery"] == "delivered"
            assert request(local, path + "/directions", {**direction, "id": uuid4().hex})[0] == 409

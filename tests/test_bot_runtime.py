from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from uuid import uuid4

from agent_office import bot_runtime
from agent_office.store import TaskStore
from test_codex_inbox import install_mock_codex
from test_meeting_flow import setup as meeting_setup
from test_studio_http import project, request, studio, wait_for
from test_workspace_rooms import engine_snapshot, register


def create_bot(server, room, bot_id):
    assert request(server, room + "/bots", {"id": bot_id, "name": bot_id, "role": "자문",
                                            "instructions": "근거를 설명한다."})[0] == 200


def test_bot_chat_survives_restart_and_supplies_only_its_own_bounded_context(tmp_path, monkeypatch, meeting_setup):
    folders, _ = meeting_setup
    log = tmp_path / "chat-inputs.jsonl"
    db = tmp_path / "office.db"
    monkeypatch.setenv("OFFICE_TEST_BOT_PROMPTS", str(log))
    with studio(db) as server:
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("claude", "antigravity"))
        workspace = register(server, folders)
        room = "/api/rooms/" + workspace
        create_bot(server, room, "historian")
        create_bot(server, room, "writer")
        path = room + "/bots/historian"
        first = {"id": uuid4().hex, "message": "설정 값 변경 SECRET=fixture " + "가" * 500 + "end-marker",
                 "allow_edits": True, "delegation": False, "test_files": ["web/test_value.py"],
                 "role_engines": {"developer": "claude"}}
        assert request(server, path + "/commands", first)[0] == 202
        wait_for(server, first["id"], {"READY_FOR_HUMAN_REVIEW"})
        # The previous client's receipts predate the conversation index. Only the
        # same direct command can recover that relationship on replay.
        with TaskStore(db, existing=True) as store, store._write():
            store.connection.execute("DELETE FROM studio_bot_turns WHERE run_id=?", (first["id"],))
        assert request(server, room + "/bots/writer/commands", first)[0] == 409
        assert request(server, path + "/chat")[1] == {"messages": [], "next_cursor": None}
        assert request(server, path + "/commands", first)[0] == 202
        status, chat = request(server, path + "/chat")
        assert status == 200
        assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
        assert all(m["run_id"] == first["id"] for m in chat["messages"])
        assert "SECRET=[redacted]" in chat["messages"][0]["text"]
        assert chat["messages"][1]["text"] == "값 변경 완료"
        assert request(server, room + "/bots/writer/chat")[1] == {"messages": [], "next_cursor": None}
        other = "/api/rooms/" + register(server, folders)
        create_bot(server, other, "historian")
        assert request(server, other + "/bots/historian/chat")[1] == {"messages": [], "next_cursor": None}
    with studio(db) as reopened:
        monkeypatch.setattr(reopened.engines, "snapshot", lambda: engine_snapshot("claude", "antigravity"))
        assert request(reopened, path + "/chat") == (200, chat)
        second = {"id": uuid4().hex, "message": "앞 답변대로 한 번 더", "allow_edits": False, "delegation": False,
                  "role_engines": {"developer": "claude"}}
        status, body = request(reopened, path + "/commands", second)
        assert status == 202 and body["run"]["task"] == second["message"]
        assert not body["run"]["allow_edits"] and body["run"]["delegation"] is None
        wait_for(reopened, second["id"], {"PLAN_ONLY"})
        prompts = [json.loads(line)[1] for line in log.read_text(encoding="utf-8").splitlines()]
        assert len(prompts) == 3
        assert "past_messages" in prompts[-1] and "값 변경 완료" in prompts[-1] and "앞 답변대로" in prompts[-1]
        assert "end-marker" not in prompts[-1] and "SECRET=fixture" not in prompts[-1]
        assert len(request(reopened, path + "/chat")[1]["messages"]) == 4


def test_confirmed_order_retries_only_missing_duties_across_clients_and_restart(tmp_path, monkeypatch):
    web, _ = project(tmp_path / "one")
    other_web, _ = project(tmp_path / "two")
    db = tmp_path / "office.db"
    launched = []
    with studio(db) as server:
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("codex"))
        monkeypatch.setattr(server.runner, "_launch", lambda _store, run, prompt: launched.append((run, prompt)))
        workspace = register(server, (web, other_web))
        room = "/api/rooms/" + workspace
        path = room + "/bot-orders"
        for bot in ("scribe", "scholar", "artist"):
            create_bot(server, room, bot)
        body = {"id": uuid4().hex, "message": "행사를 준비하라 SECRET=private-order", "allow_edits": False,
                "assignments": [{"bot_id": "scribe", "duty": "기록 TOKEN=private-duty"}, {"bot_id": "scholar", "duty": "조사"},
                                {"bot_id": "artist", "duty": "그림"}]}
        assert request(server, path, body)[0] == 400
        body["indices"] = [1]
        unknown = {"bot_id": "missing", "duty": "기타"}
        assert request(server, path, {**body, "assignments": [body["assignments"][0], unknown]})[0] == 404
        assert request(server, path, {**body, "assignments": [body["assignments"][0]] * 2})[0] == 400
        assert request(server, room + "/bots/assignment", {"role": "reviewer", "id": "scholar"})[0] == 200
        assert request(server, path, {**body, "allow_edits": True})[0] == 400
        assert request(server, path) == (200, {"orders": [], "next_cursor": None})
        assert not launched
        assign = server.rooms.assign

        def unavailable(workspace_id, data, **kwargs):
            if kwargs["target_bot"] == "scholar":
                raise ValueError("잠시 후 재시도하세요.")
            receipt = assign(workspace_id, data, **kwargs)
            if kwargs["target_bot"] == "artist":
                raise TimeoutError("lost receipt after durable admission")
            return receipt

        monkeypatch.setattr(server.rooms, "assign", unavailable)
        assert request(server, path, body)[0] == 503
        order = request(server, path)[1]["orders"][0]
        assert order["indices"] == [1] and order["message"] == "행사를 준비하라 SECRET=[redacted]"
        assert order["replay_requires_original"] is True
        assert "private-order" not in str(order) and "private-duty" not in str(order)
        assert [a["state"] for a in order["assignments"]] == ["QUEUED", "pending", "QUEUED"]
        assert order["assignments"][1]["error"] == "잠시 후 재시도하세요."
        assert len(launched) == 2
        assert request(server, path, {**body, "allow_edits": True})[0] == 409
        public_duties = [{"bot_id": item["bot_id"], "duty": item["duty"]} for item in order["assignments"]]
        assert request(server, path, {**body, "message": order["message"], "assignments": public_duties})[0] == 409
        assert request(server, path, {**body, "assignments": body["assignments"][::-1]})[0] == 409
        assert request(server, path)[1]["orders"] == [order]
        other = "/api/rooms/" + register(server, (web,)) + "/bot-orders"
        assert request(server, other) == (200, {"orders": [], "next_cursor": None})
    with studio(db) as reopened:
        monkeypatch.setattr(reopened.engines, "snapshot", lambda: engine_snapshot("codex"))
        monkeypatch.setattr(reopened.runner, "_launch", lambda _store, run, prompt: launched.append((run, prompt)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: request(reopened, path, body), range(2)))
        assert all(status == 202 for status, _ in result)
        duties = result[0][1]["order"]["assignments"]
        assert duties == result[1][1]["order"]["assignments"]
        assert [d["run_id"] for d in duties] == [a["run_id"] for a in order["assignments"]]
        assert all(d["state"] != "pending" and "error" not in d for d in duties)
        assert len(launched) == len(set(run for run, _ in launched)) == 3
        for duty in duties:
            run = request(reopened, "/api/runs/" + duty["run_id"])[1]["run"]
            assert run["assignment"]["bots"]["developer"]["id"] == duty["bot_id"]
            assert "행사를 준비하라" in run["task"] and duty["duty"] in run["task"]
            assert not run["allow_edits"] and run["delegation"] is None
            assert request(reopened, room + "/bots/" + duty["bot_id"] + "/chat")[1]["messages"][0]["run_id"] == run["id"]


def test_readonly_order_starts_two_workers_and_retains_their_permission_gates(tmp_path, monkeypatch):
    tools = tmp_path / "mock provider"
    tools.mkdir()
    install_mock_codex(tools, monkeypatch, "read-only")
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    folders = project(tmp_path)
    with studio(tmp_path / "office.db") as server:
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("codex"))
        room = "/api/rooms/" + register(server, folders)
        create_bot(server, room, "scribe")
        create_bot(server, room, "artist")
        status, response = request(server, room + "/bot-orders", {
            "id": uuid4().hex, "message": "원래 작업", "allow_edits": False,
            "assignments": [{"bot_id": "scribe", "duty": "기록 정리"}, {"bot_id": "artist", "duty": "그림 검토"}]})
        assert status == 202, response
        assert response["order"]["replay_requires_original"] is False
        trees = set()
        for duty in response["order"]["assignments"]:
            run = wait_for(server, duty["run_id"], {"PLAN_ONLY"})
            assert run["developer_output"] == "작업 완료"
            assert run["assignment"]["bots"]["developer"]["id"] == duty["bot_id"]
            tree = Path(run["workspace"])
            trees.add(tree)
            assert tree.is_dir() and not (tree / "answer.txt").exists()
            assert request(server, room + "/bots/" + duty["bot_id"] + "/chat")[1]["messages"][-1]["text"] == "작업 완료"
        assert len(trees) == 2
        final = request(server, room + "/bot-orders")[1]["orders"][0]
        assert [a["state"] for a in final["assignments"]] == ["PLAN_ONLY", "PLAN_ONLY"]


def test_history_cursor_keeps_its_scope_and_excludes_unfinished_or_failed_replies(tmp_path, monkeypatch):
    db = tmp_path / "office.db"
    folders = project(tmp_path)
    monkeypatch.setattr(bot_runtime, "CHAT_PAGE", 2)
    monkeypatch.setattr(bot_runtime, "ORDER_PAGE", 2)
    with studio(db) as server:
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("codex"))

        def partial_output(store, run, _prompt):
            store.connection.execute("UPDATE studio_runs SET developer_output=? WHERE id=?", ("partial/diagnostic", run))

        monkeypatch.setattr(server.runner, "_launch", partial_output)
        room = "/api/rooms/" + register(server, folders)
        other = "/api/rooms/" + register(server, folders)
        create_bot(server, room, "scribe")
        create_bot(server, room, "writer")
        create_bot(server, other, "scribe")
        chat, orders = room + "/bots/scribe/chat", room + "/bot-orders"

        def send():
            data = {"id": uuid4().hex, "message": "확인", "allow_edits": False,
                    "assignments": [{"bot_id": "scribe", "duty": "기록"}]}
            status, response = request(server, orders, data)
            assert status == 202
            return data["id"], response["order"]["assignments"][0]["run_id"]

        first, failed = send()
        with TaskStore(db, existing=True) as store:
            store.record_transition(failed, operation_key="fixture:failed", event_type="RUN_FINISHED",
                                    state="FAILED", outcome="DEVELOPER_FAILED")
        second, open_run = send()
        third, recent = send()
        page = request(server, chat)[1]
        assert [(m["role"], m["run_id"]) for m in page["messages"]] == [("user", open_run), ("user", recent)]
        assert page["next_cursor"] == open_run
        listing = request(server, orders)[1]
        assert [order["id"] for order in listing["orders"]] == [third, second]
        assert listing["next_cursor"] == second
        send()  # New admissions do not shift a cursor into already displayed rows.
        older = request(server, chat + "/before/" + open_run)[1]
        assert [(m["run_id"], m["role"], m["state"]) for m in older["messages"]] == [(failed, "user", "FAILED")]
        assert older["next_cursor"] is None
        older = request(server, orders + "/before/" + second)[1]
        assert [entry["id"] for entry in older["orders"]] == [first] and older["next_cursor"] is None
        assert request(server, other + "/bot-orders/before/" + second)[0] == 404
        assert request(server, other + "/bots/scribe/chat/before/" + open_run)[0] == 404
        assert request(server, room + "/bots/writer/chat/before/" + open_run)[0] == 404
        with TaskStore(db, readonly=True) as store:
            assert bot_runtime.prior_context(store.connection, recent) == []

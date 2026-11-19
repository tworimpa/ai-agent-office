"""Synthetic stdio app-server, exercising the same launcher entry point as Studio."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from agent_office.adapters import CLIAdapter


class Operator:
    def __init__(self, action="approve", delay=False):
        self.action = action
        self.delay = delay
        self.requests = []
        self.sent = []
        self.confirmed = []

    async def ask(self, kind, prompt, session_id):
        assert session_id == "thread"
        self.requests.append((kind, prompt))
        if self.delay:
            await asyncio.sleep(10)
        return {"decision": "answer" if kind == "question" else self.action,
                "answer": "둘째 방식" if kind == "question" else None, "id": str(len(self.requests))}

    @contextmanager
    def sending(self, request_id):
        assert request_id not in self.sent
        self.sent.append(request_id)
        yield

    def delivered(self, request_id):
        assert request_id in self.sent and request_id not in self.confirmed
        self.confirmed.append(request_id)


def install_mock_codex(directory, monkeypatch, scenario):
    executable = directory / ("codex.cmd" if os.name == "nt" else "codex")
    if os.name == "nt":
        executable.write_text(f'@"{sys.executable}" "{__file__}" %*\n')
    else:
        executable.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(__file__)} "$@"\n')
        executable.chmod(0o700)
    monkeypatch.setenv("OFFICE_FIXTURE_CODEX", scenario)
    return executable


def fixture(tmp_path, monkeypatch, scenario, edits=True, operator=None):
    executable = install_mock_codex(tmp_path, monkeypatch, scenario)
    monkeypatch.setattr("agent_office.adapters.resolve_executable", lambda _: str(executable))
    operator = operator or Operator()
    result = CLIAdapter("codex", timeout=2 if scenario == "timeout" else 8).execute(
        "원래 작업", workspace=tmp_path, allow_edits=edits, inbox=operator)
    return operator, result


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_same_thread_answer_then_exact_file_decision(tmp_path, monkeypatch, action):
    operator, result = fixture(tmp_path, monkeypatch, action, operator=Operator(action))
    assert (result.returncode, result.session_id, result.message) == (0, "thread", "작업 완료")
    assert [kind for kind, _ in operator.requests] == ["question", "permission"]
    assert operator.requests[0][1] == "어느 방식?"
    details = json.loads(operator.requests[1][1])
    assert details["changes"] == [{"path": str(tmp_path / "answer.txt"),
                                   "kind": {"type": "add"}, "diff": "+fixture\n"}]
    assert operator.confirmed == operator.sent == ["1", "2"]
    assert (tmp_path / "answer.txt").exists() == (action == "approve")


@pytest.mark.parametrize("scenario", ["read-only", "grant-root", "outside", "move-outside", "hidden-secret", "command"])
def test_no_approval_for_unsupported_scope(tmp_path, monkeypatch, scenario):
    operator, result = fixture(tmp_path, monkeypatch, scenario, edits=scenario != "read-only")
    assert result.returncode == 0 and result.message == "작업 완료"
    assert not operator.requests and not (tmp_path / "answer.txt").exists()


@pytest.mark.parametrize("scenario", ["bad-mode", "withdrawn", "changed-diff",
                                      "wrong-thread", "wrong-turn", "unexpected-receipt", "failed-turn", "no-final", "ignored-command-denial", "false-marker", "trailing-turn", "buffered-trailing"])
def test_stale_or_ambiguous_provider_cannot_complete(tmp_path, monkeypatch, scenario):
    operator, result = fixture(tmp_path, monkeypatch, scenario, operator=Operator(delay=scenario == "withdrawn"))
    assert result.returncode and not operator.confirmed
    assert not (tmp_path / "answer.txt").exists()
    if scenario == "withdrawn":
        assert not operator.sent


def test_native_connector_question_gets_no_authorization_and_technical_turn_can_continue(tmp_path, monkeypatch):
    operator, result = fixture(tmp_path, monkeypatch, "native-input")
    assert result.returncode == 0 and result.message == "작업 완료"
    assert operator.requests == [("question", "일정은?")] and operator.confirmed == ["1"]


def test_readonly_codex_handles_remote_control_status_during_startup(tmp_path, monkeypatch):
    operator, result = fixture(tmp_path, monkeypatch, "remote-status", edits=False)
    assert (result.returncode, result.session_id, result.message) == (0, "thread", "작업 완료")
    assert not operator.requests and not operator.confirmed


def test_timeout_expires_unsent_codex_decision(tmp_path, monkeypatch):
    operator, result = fixture(tmp_path, monkeypatch, "timeout", operator=Operator(delay=True))
    assert result.returncode == 124 and not operator.sent and not operator.confirmed


def server():
    scenario = os.environ["OFFICE_FIXTURE_CODEX"]
    if sys.argv[1:2] == ["exec"] and scenario in ("room-choice", "room-product"):
        from helpers.away_roles import main

        sys.argv[1] = "codex"
        main()
        return
    assert sys.argv[1:] == ["app-server", "--listen", "stdio://"]

    def read():
        return json.loads(sys.stdin.buffer.readline())

    def emit(**record):
        print(json.dumps(record, ensure_ascii=True), flush=True)

    def item(method, data):
        emit(method="item/" + method, params={"threadId": "thread", "turnId": turn, "item": data})

    def agent(text, phase="final_answer"):
        data = {"id": "agent" + turn, "type": "agentMessage", "phase": phase}
        item("started", {**data, "text": ""})
        item("completed", {**data, "text": text})

    def end():
        emit(method="turn/completed", params={"threadId": "thread", "turn": {
            "id": turn, "items": [], "status": "failed" if scenario == "failed-turn" else "completed"}})

    def start_turn(first, direction=False):
        start = read()
        assert start["method"] == "turn/start" and start["params"]["threadId"] == "thread"
        message = start["params"]["input"][0]["text"]
        if direction:
            marker = "\nOperator directions (JSON array):\n"
            if marker in message:
                directions = message.split(marker, 1)[1].split("\n\n", 1)[0]
                assert json.loads(directions) == ['중간 지시 & "원문"']
            else:
                assert '중간 지시 & "원문"' in message
        else:
            answer = os.environ.get("OFFICE_FIXTURE_ROOM_ANSWER", "둘째 방식") if scenario.startswith("room-") else "둘째 방식"
            assert ("원래 작업" if first else answer) in message
        if first:
            assert "AGENT_OFFICE_QUESTION" in message
        turn = "1" if first else "3" if direction else "2"
        if direction and scenario == "direction-lost":
            return turn
        user = {"type": "userMessage", "id": "user" + turn}
        for method in ("item/started", "item/completed"):
            emit(method=method, params={"threadId": "thread", "turnId": turn, "item": user})
        if scenario == "buffered-trailing":
            final = {"id": "final", "type": "agentMessage", "phase": "final_answer", "text": "done"}
            for method in ("item/started", "item/completed"):
                emit(method=method, params={"threadId": "thread", "turnId": turn, "item": final})
            for _ in range(2):
                emit(method="turn/completed", params={"threadId": "thread", "turn": {"id": turn, "status": "completed"}})
        emit(id=start["id"], result={"turn": {"id": turn, "items": [], "status": "inProgress"}})
        return turn

    init = read()
    assert init["method"] == "initialize"
    emit(id=init["id"], result={"userAgent": "fixture"})
    if scenario == "remote-status":
        emit(method="remoteControl/status/changed", params={"status": "disabled", "environmentId": None})
    assert read()["method"] == "initialized"
    start = read()
    params = start["params"]
    assert start["method"] == "thread/start" and params["approvalsReviewer"] == "user"
    assert params["approvalPolicy"] == "on-request" and Path(params["cwd"]) == Path.cwd()
    read_only = scenario in ("read-only", "direction-plan", "room-choice", "room-product", "remote-status")
    assert params["sandbox"] == ("read-only" if read_only else "workspace-write")
    policy = {"type": "readOnly"} if read_only else {
        "type": "workspaceWrite", "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}
    if scenario == "bad-mode":
        policy["type"] = "dangerFullAccess"
    emit(id=start["id"], result={"sandbox": policy, "cwd": str(Path.cwd()), "approvalsReviewer": "user",
                                 "approvalPolicy": "on-request", "thread": {"id": "thread"}})
    turn = start_turn(True)
    if scenario == "buffered-trailing":
        assert not sys.stdin.buffer.readline()
        return
    if scenario in ("approve", "reject", "direction", "direction-lost", "direction-plan"):
        agent("AGENT_OFFICE_QUESTION\n어느 방식?")
        end()
        turn = start_turn(False)
    if scenario in ("room-choice", "room-product"):
        if scenario == "room-choice":
            choice = {"purpose": "diagnosis", "options": [{"path": "web/first.py", "line": 1},
                                                            {"path": "web/second.py", "line": 1}]}
            agent("AGENT_OFFICE_QUESTION\nAGENT_OFFICE_TECHNICAL_CHOICES\n" + json.dumps(choice))
        else:
            agent("AGENT_OFFICE_PRODUCT_DECISION\n기본 표시 동작을 바꿀까요?")
        end()
        turn = start_turn(False)
    for method in ("item/commandExecution/terminalInteraction", "model/verification", "model/safetyBuffering/updated"):
        emit(method=method, params={"threadId": "thread", "turnId": turn})
    progress = {"id": "async", "type": "agentMessage", "phase": "final_answer", "delivery": "async"}
    item("started", {**progress, "text": ""})
    item("completed", {**progress, "text": "AGENT_OFFICE_QUESTION\nnot a blocking question"})
    scope = {"threadId": "thread", "turnId": turn, "itemId": "file"}
    if scenario == "native-input":
        emit(method="item/tool/requestUserInput", id=71, params={**scope, "questions": [
            {"id": "app", "header": "tool", "question": "Execute?", "options": [{"label": "Accept"}]}]})
        assert read() == {"id": 71, "result": {"answers": {}}}
        emit(method="serverRequest/resolved", params={"threadId": "thread", "requestId": 71})
        agent("AGENT_OFFICE_QUESTION\n일정은?")
        end()
        turn = start_turn(False)
    if scenario in ("command", "ignored-command-denial"):
        command = {"type": "commandExecution", "id": "file", "command": "untrusted", "cwd": str(Path.cwd())}
        item("started", {**command, "status": "inProgress"})
        emit(method="item/commandExecution/requestApproval", id=71, params=scope)
        assert read() == {"id": 71, "result": {"decision": "decline"}}
        emit(method="serverRequest/resolved", params={"threadId": "thread", "requestId": 71})
        item("completed", {**command, "status": "declined" if scenario == "command" else "completed"})
    elif scenario in ("approve", "reject", "read-only", "grant-root", "outside", "move-outside",
                      "hidden-secret", "withdrawn", "changed-diff", "wrong-turn", "unexpected-receipt", "timeout",
                      "direction", "direction-lost"):
        grant = {"grantRoot": str(Path.cwd())} if scenario == "grant-root" else {}
        target = Path.cwd() / "answer.txt"
        outside = str(Path.cwd().parent / "outside")
        kind = {"type": "update", "move_path": outside} if scenario == "move-outside" else {"type": "add"}
        changes = [{"path": outside if scenario == "outside" else str(target), "kind": kind,
                    "diff": '+{"password":"fixture"}' if scenario == "hidden-secret" else "+fixture\n"}]
        data = {"type": "fileChange", "id": "file", "changes": changes}
        emit(method="item/fileChange/patchUpdated", params={**scope, "changes": [{"incomplete": True}]})
        item("started", {**data, "status": "inProgress"})
        if scenario == "wrong-turn":
            scope["turnId"] = "previous"
        emit(id=71, method="item/fileChange/requestApproval", params={**scope, **grant})
        if scenario == "withdrawn":
            emit(method="serverRequest/resolved", params={"threadId": "thread", "requestId": 71})
            assert not sys.stdin.buffer.readline()
            return
        decision = read()["result"]["decision"]
        assert decision == ("accept" if scenario in ("approve", "changed-diff", "unexpected-receipt", "direction",
                                                     "direction-lost") else "decline")
        if scenario in ("approve", "direction", "direction-lost"):
            target.write_text("fixture", encoding="utf-8")
        if scenario == "changed-diff":
            changes[0]["diff"] += "+unseen"
        emit(method="serverRequest/resolved", params={"threadId": "thread",
                                                     "requestId": 999 if scenario == "unexpected-receipt" else 71})
        item("completed", {**data, "status": "completed" if decision == "accept" else "declined"})
    if scenario == "wrong-thread":
        emit(method="item/completed", params={"threadId": "other", "turnId": turn, "item": {}})
    if scenario == "false-marker":
        agent("prefixAGENT_OFFICE_FINAL", None)
    else:
        agent("작업 완료", "commentary" if scenario == "no-final" else "final_answer")
    end()
    if scenario in ("direction", "direction-lost", "direction-plan"):
        turn = start_turn(False, direction=True)
        if scenario == "direction-lost":
            return
        agent("지시 반영")
        end()
    assert not sys.stdin.buffer.readline()
    if scenario == "trailing-turn":
        end()
    emit(method="thread/closed", params={"threadId": "thread"})


if __name__ == "__main__":
    server()

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


def fixture(tmp_path, monkeypatch, scenario, edits=True, operator=None, **options):
    executable = install_mock_codex(tmp_path, monkeypatch, scenario)
    monkeypatch.setattr("agent_office.adapters.resolve_executable", lambda _: str(executable))
    operator = operator or Operator()
    result = CLIAdapter("codex", timeout=2 if scenario == "timeout" else 8).execute(
        "원래 작업", workspace=tmp_path, allow_edits=edits, inbox=operator, **options)
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


@pytest.mark.parametrize("scenario", ["remote-status", "account-status"])
def test_readonly_codex_handles_status_during_startup(tmp_path, monkeypatch, scenario):
    operator, result = fixture(tmp_path, monkeypatch, scenario, edits=False)
    assert (result.returncode, result.session_id, result.message) == (0, "thread", "작업 완료")
    assert not operator.requests and not operator.confirmed


@pytest.mark.parametrize("scenario", ["late-ok", "late-failed", "late-id", "late-thread", "late-turn",
                                      "late-permission", "late-no-exit"])
def test_late_command_receipts_bind_to_the_existing_turn(tmp_path, monkeypatch, scenario):
    operator, result = fixture(tmp_path, monkeypatch, scenario)
    assert (result.returncode == 0) == (scenario == "late-ok")
    assert not operator.requests


@pytest.mark.parametrize("scenario", ["service-shell", "service-shell-legacy", "service-shell-rejected"])
def test_windows_service_supplies_shell_context_and_preserves_approval(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr("agent_office.codex_inbox.windows_service_session", lambda: True)
    operator, result = fixture(tmp_path, monkeypatch, scenario, edits=False)
    if scenario == "service-shell-rejected":
        assert result.returncode == 1
        assert result.message == "Codex app-server [thread/inject_items] protocol: RPC rejected (code=-32602)."
    else:
        assert (result.returncode, result.session_id, result.message) == (0, "thread", "작업 완료")
    assert not operator.requests


@pytest.mark.parametrize(("scenario", "diagnosis"), [
    ("rpc-rejected", "Codex app-server [thread/start] protocol: RPC rejected (code=-32602)."),
    ("startup-notification", "Codex app-server [thread/start] protocol: unexpected notification (windows/worldWritableWarning)."),
    ("unknown-startup-notification", "Codex app-server [thread/start] protocol: unexpected notification (unrecognized)."),
    ("account-status-request", "Codex app-server [thread/start] protocol: unexpected server request (account/updated)."),
    ("bad-mode", "Codex app-server [thread/start] protocol: requested sandbox mode not confirmed."),
    ("failed-turn", "Codex app-server [turn/completed] protocol: provider turn failed (unauthorized)."),
])
def test_protocol_diagnosis_records_only_fixed_metadata(tmp_path, monkeypatch, scenario, diagnosis):
    operator, result = fixture(tmp_path, monkeypatch, scenario)
    assert result.returncode == 1 and result.message == diagnosis
    assert not operator.requests and not operator.confirmed


def test_timeout_expires_unsent_codex_decision(tmp_path, monkeypatch):
    operator, result = fixture(tmp_path, monkeypatch, "timeout", operator=Operator(delay=True))
    assert result.returncode == 124 and not operator.sent and not operator.confirmed


def test_task_profile_bound_telemetry_and_account_change(tmp_path, monkeypatch):
    events = []
    _, result = fixture(tmp_path, monkeypatch, "profile", model="fixture-model", effort="high", telemetry=events.append)
    assert result.returncode == 0, result.message
    assert events[0] == {"kind": "model", "actual_model": "fixture-model", "actual_effort": "high"}
    assert events[1]["ordinary_usage_allowed"] is True and len(events[1]["account_key"]) == 64
    assert events[2] == {"kind": "model", "actual_model": "fixture-reroute"}
    assert events[3] == {"kind": "usage", "input_tokens": 700, "total_tokens": 900,
                         "context_used": 300, "context_basis": "last_response_total", "context_window": 4000}
    assert events[4]["account_key"] == events[1]["account_key"]
    assert "account_key" not in events[5]
    assert "private" not in json.dumps(events)


def test_native_preflight_exhaustion_closes_before_first_model_turn(tmp_path, monkeypatch):
    events = []
    _, result = fixture(tmp_path, monkeypatch, "exhausted", telemetry=events.append)
    assert result.preflight_blocked and result.returncode == 75
    assert events[-1] == {"kind": "usage", "total_tokens": 0}
    assert events[-2]["ordinary_usage_allowed"] is False


@pytest.mark.parametrize("scenario", ["catalog", "catalog-bad"])
def test_catalog_refresh_only_invokes_paginated_metadata(tmp_path, monkeypatch, scenario):
    from agent_office.provider_profiles import ModelCatalog
    executable = install_mock_codex(tmp_path, monkeypatch, scenario)
    monkeypatch.setattr("agent_office.provider_profiles.resolve_executable", lambda _: str(executable))
    catalog = ModelCatalog()
    assert catalog.read()["status"] == "unprobed"
    snapshot = catalog.refresh()
    if scenario == "catalog-bad":
        assert snapshot["status"] == "unavailable" and snapshot["models"] == []
        return
    assert [row["id"] for row in snapshot["models"]] == ["visible-1", "visible-2"]
    assert snapshot["models"][0]["efforts"] == ["high"]
    snapshot["models"].clear()
    catalog.validate("codex", "visible-2", "high")
    with pytest.raises(ValueError):
        catalog.validate("codex", "visible-2", "low")
    assert "private" not in json.dumps(catalog.read())


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
        completed = {"id": turn, "items": [], "status": "completed"}
        if scenario == "failed-turn":
            completed.update(status="failed", error={"message": "provider-private-text", "codexErrorInfo": "unauthorized"})
        emit(method="turn/completed", params={"threadId": "thread", "turn": completed})

    def start_turn(first, direction=False):
        start = read()
        if first and not scenario.startswith("service-shell") and start["method"] == "thread/inject_items":
            assert start["params"]["threadId"] == "thread"
            emit(id=start["id"], result={})
            start = read()
        assert start["method"] == "turn/start" and start["params"]["threadId"] == "thread"
        if scenario == "profile":
            assert start["params"]["model"] == "fixture-model" and start["params"]["effort"] == "high"
        message = start["params"]["input"][0]["text"]
        if scenario.startswith("service-shell"):
            assert ("exec_command" in message and "cmd.exe" in message) == (scenario == "service-shell-legacy")
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
        if scenario == "profile":
            emit(method="model/rerouted", params={"threadId": "thread", "turnId": turn, "toModel": "fixture-reroute"})
            emit(method="thread/tokenUsage/updated", params={"threadId": "thread", "turnId": turn,
                 "tokenUsage": {"total": {"inputTokens": 700, "totalTokens": 900},
                                "last": {"totalTokens": 300}, "modelContextWindow": 4000}})
            emit(method="account/rateLimits/updated", params={"rateLimits": quota})
            emit(method="account/updated", params={"private": "private"})
            emit(method="account/rateLimits/updated", params={"rateLimits": quota})
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
    if scenario.startswith("catalog"):
        for page in (1, 2):
            assert start["method"] == "model/list" and start["params"]["includeHidden"] is False
            assert start["params"].get("cursor") == ("page2" if page == 2 else None)
            row = {"model": f"visible-{page}", "displayName": "Fixture", "description": "private",
                   "supportedReasoningEfforts": [{"reasoningEffort": "high", "description": "private"}],
                   "defaultReasoningEffort": "high", "isDefault": page == 1}
            emit(id=start["id"], result={"data": [row, {**row, "hidden": True}],
                 "nextCursor": "page2" if page == 1 or scenario == "catalog-bad" else None})
            if page == 1:
                start = read()
        return
    params = start["params"]
    assert start["method"] == "thread/start" and params["approvalsReviewer"] == "user"
    assert params["approvalPolicy"] == "on-request" and Path(params["cwd"]) == Path.cwd()
    if scenario == "profile":
        assert params["model"] == "fixture-model" and params["config"]["model_reasoning_effort"] == "high"
        skip = init["params"]["capabilities"]["optOutNotificationMethods"]
        assert "model/rerouted" not in skip and "thread/tokenUsage/updated" not in skip
    if scenario == "account-status":
        emit(method="account/updated", params={"authMode": "chatgpt", "planType": "plus"})
    if scenario == "account-status-request":
        emit(id=42, method="account/updated", params={"authMode": "chatgpt", "planType": "plus"})
        return
    if scenario == "rpc-rejected":
        emit(id=start["id"], error={"code": -32602, "message": "provider-private-text", "data": "private-response"})
        return
    if scenario in ("startup-notification", "unknown-startup-notification"):
        topic = "windows/worldWritableWarning" if scenario == "startup-notification" else "unexpected/private-method-text"
        emit(method=topic, params={"path": "provider-private-text"})
        return
    read_only = scenario.startswith("service-shell") or scenario in (
        "read-only", "direction-plan", "room-choice", "room-product", "remote-status", "account-status")
    assert params["sandbox"] == ("read-only" if read_only else "workspace-write")
    policy = {"type": "readOnly"} if read_only else {
        "type": "workspaceWrite", "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}
    if scenario == "bad-mode":
        policy["type"] = "dangerFullAccess"
    profile = ({"modelProvider": "openai", "model": "fixture-model", "reasoningEffort": "high"}
               if scenario in ("profile", "exhausted") else {})
    emit(id=start["id"], result={"sandbox": policy, "cwd": str(Path.cwd()), "approvalsReviewer": "user", **profile,
                                 "approvalPolicy": "on-request", "thread": {"id": "thread"}})
    if scenario in ("profile", "exhausted"):
        request = read()
        assert request["method"] == "account/rateLimits/read"
        quota = {"limitId": "codex", "primary": {"usedPercent": 20, "windowDurationMins": 300}}
        emit(id=request["id"], result={"accountId": "private-account", "ordinaryUsageAllowed": scenario != "exhausted",
                                       "rateLimits": quota})
        if scenario == "exhausted":
            assert not sys.stdin.buffer.readline()
            return
    if scenario.startswith("service-shell"):
        inject = read()
        assert inject["method"] == "thread/inject_items" and inject["params"]["threadId"] == "thread"
        context, = inject["params"]["items"]
        assert context["type"] == "message" and context["role"] == "developer"
        command, = context["content"]
        assert command["type"] == "input_text"
        assert all(part in command["text"] for part in ("cmd.exe", "exec_command", "login", "tty", "Git", "sandbox"))
        if scenario == "service-shell":
            emit(id=inject["id"], result={})
        else:
            code = -32601 if scenario == "service-shell-legacy" else -32602
            emit(id=inject["id"], error={"code": code, "message": "provider refused context"})
            if scenario == "service-shell-rejected":
                return
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
    if scenario.startswith("late-"):
        item("started", {"id": "pending", "type": "commandExecution", "status": "inProgress"})
    if scenario == "false-marker":
        agent("prefixAGENT_OFFICE_FINAL", None)
    else:
        agent("작업 완료", "commentary" if scenario == "no-final" else "final_answer")
    end()
    if scenario.startswith("late-"):
        completed = {"id": "other" if scenario == "late-id" else "pending", "type": "commandExecution",
                     "status": "completed", "exitCode": 1 if scenario == "late-failed" else 0}
        if scenario == "late-no-exit":
            completed.pop("exitCode")
        payload = {"threadId": "other" if scenario == "late-thread" else "thread",
                   "turnId": "other" if scenario == "late-turn" else turn, "item": completed}
        if scenario == "late-permission":
            emit(id=70, method="item/commandExecution/requestApproval", params=payload)
        else:
            emit(method="item/completed", params=payload)
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

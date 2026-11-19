"""Opt-in Studio transport for the Codex v2 app-server stdio protocol."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import os
from pathlib import Path

from .acp import ProtocolError, _file_target, _unique_fields, close_stdio_process, write_record
from .adapters import AgentResult
from .dialogue import (FINAL_MARKER, MAX_QUESTIONS, PRODUCT_DECISION_MARKER, QUESTION_INSTRUCTIONS, QUESTION_MARKER,
                       continuation_prompt, extract_request)
from .events import snapshot_request
from .dialogue import DelegationWithdrawn
from .models import OfficeError


# These notifications cannot resolve a request, alter a patch or finish a turn.
INFORMATIONAL = {
    "account/rateLimits/updated", "configWarning", "warning", "deprecationNotice",
    "mcpServer/startupStatus/updated", "thread/status/changed", "thread/tokenUsage/updated",
    "thread/readState/changed", "hook/started", "hook/completed",
    "turn/diff/updated", "turn/plan/updated", "item/agentMessage/delta",
    "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
    "item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded", "item/reasoning/textDelta",
    "item/plan/delta", "item/mcpToolCall/progress", "item/commandExecution/terminalInteraction",
    "model/rerouted", "model/verification", "model/safetyBuffering/updated",
}


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ProtocolError("missing Codex protocol ID")
    return value


def _object(value):
    if not isinstance(value, dict):
        raise ProtocolError("invalid Codex protocol object")
    return value


def _changes(workspace, changes):
    """Require the full native preview, including both ends of a move."""
    if not isinstance(changes, list) or not 0 < len(changes) <= 100:
        return False
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"path", "diff", "kind"}:
            return False
        diff, kind = change["diff"], change["kind"]
        if not isinstance(diff, str) or not diff or snapshot_request(diff) != diff or not isinstance(kind, dict):
            return False
        paths = [change["path"]]
        if kind.get("type") == "update" and set(kind) <= {"type", "move_path"}:
            if kind.get("move_path") is not None:
                paths.append(kind["move_path"])
        elif kind not in ({"type": "add"}, {"type": "delete"}):
            return False
        for target in paths:
            if not isinstance(target, str) or not target or snapshot_request(target) != target:
                return False
            path = workspace / target
            if not _file_target(workspace, str(path), writing=True):
                return False
            if any(part.casefold() in (".codex", ".agents") for part in path.relative_to(workspace).parts):
                return False
    return True


class CodexSession:
    def __init__(self, process, workspace, edits, inbox, deadline):
        self.deadline = deadline
        self.process = process
        self.workspace = workspace
        self.edits = edits
        self.inbox = inbox
        self.directions = getattr(inbox, "directions", None)
        self.thread = None
        self.turn = None
        self.records = 0
        self.early = deque()
        self.last_size = 0
        self.client_id = 0
        self.server_ids = set()
        self.turn_ids = set()

    async def send(self, message, decision=None, *, direction=None):
        data = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        await write_record(self.process, data, inbox=self.inbox, decision_id=decision, deadline=self.deadline,
                           direction=direction)

    async def read(self, *, allow_eof=False):
        self.records += 1
        if self.records > 50_000:
            raise ProtocolError("Codex record limit exceeded")
        line = await self.process.stdout.readline()
        if not line:
            if allow_eof:
                return None
            raise ProtocolError("Codex disconnected")
        self.last_size = len(line)
        msg = _object(json.loads(line, object_pairs_hook=_unique_fields))
        if "method" in msg:
            _object(msg.get("params"))
            if not isinstance(msg["method"], str) or "result" in msg or "error" in msg:
                raise ProtocolError("invalid Codex event")
        return msg

    def check_binding(self, msg):
        params = msg["params"]
        if "threadId" in params and params["threadId"] != self.thread:
            raise ProtocolError("wrong Codex thread")
        if "turnId" in params and params["turnId"] != self.turn:
            raise ProtocolError("wrong Codex turn")

    async def event(self):
        while True:
            msg = self.early.popleft()[0] if self.early else await self.read()
            if "method" not in msg:
                return msg
            self.check_binding(msg)
            if "id" in msg or msg["method"] not in INFORMATIONAL:
                return msg

    async def rpc(self, method, params, decision=None, *, direction=None):
        self.client_id += 1
        await self.send({"method": method, "params": params, "id": self.client_id}, decision, direction=direction)
        if method == "turn/start":
            # Core can emit item events before the independent RPC worker
            # replies. Bind them to the acknowledged turn before acting on any.
            size = 0
            msg = await self.read()
            while "method" in msg:
                size += self.last_size
                if len(self.early) >= 128 or size > 1024 * 1024:
                    raise ProtocolError("too many unbound Codex events")
                self.early.append((msg, self.last_size))
                msg = await self.read()
        else:
            msg = await self.event()
        if ("method" in msg or type(msg.get("id")) is not int or msg["id"] != self.client_id
                or "error" in msg):
            raise ProtocolError("Codex request was not acknowledged")
        return _object(msg.get("result"))

    async def ask(self, kind, prompt):
        return await self.wait_decision(self.inbox.ask(kind, prompt, self.thread))

    async def wait_decision(self, awaitable):
        # Reject withdrawal, patch mutation, server exit or turn completion
        # while the browser is open. No new turn may inherit an old request.
        answer = asyncio.create_task(awaitable)
        invalidation = asyncio.create_task(self.event())
        try:
            await asyncio.wait((answer, invalidation), return_when=asyncio.FIRST_COMPLETED)
            if invalidation.done():
                invalidation.result()
                raise ProtocolError("Codex changed state while waiting for the operator")
            return answer.result()
        finally:
            for task in (answer, invalidation):
                task.cancel()
            await asyncio.gather(answer, invalidation, return_exceptions=True)

    def check_sandbox(self, response):
        sandbox = _object(response.get("sandbox"))
        if (response.get("approvalsReviewer") != "user" or response.get("approvalPolicy") != "on-request"
                or not isinstance(response.get("cwd"), str) or Path(response["cwd"]).resolve() != self.workspace
                or sandbox.get("type") != ("workspaceWrite" if self.edits else "readOnly")
                or sandbox.get("networkAccess", False) is not False):
            raise ProtocolError("Codex did not confirm the requested sandbox and human reviewer")
        if self.edits:
            roots = sandbox.get("writableRoots", [])
            if (not isinstance(roots, list) or any(not isinstance(p, str) or not Path(p).is_absolute()
                                                   or not Path(p).resolve().is_relative_to(self.workspace) for p in roots)
                    or sandbox.get("excludeSlashTmp") is not True or sandbox.get("excludeTmpdirEnvVar") is not True):
                raise ProtocolError("Codex advertised writable locations outside the worktree")

    async def finish(self):
        self.process.stdin.close()
        total = 0
        for _ in range(128):
            if self.early:
                msg, size = self.early.popleft()
            else:
                msg = await self.read(allow_eof=True)
                size = self.last_size
            if msg is None:
                if await self.process.wait() != 0:
                    raise ProtocolError("Codex exited unsuccessfully")
                return
            total += size
            if ("id" in msg or "method" not in msg or total > 1024 * 1024
                    or msg["method"] not in INFORMATIONAL | {"thread/closed"}):
                raise ProtocolError("unexpected Codex shutdown record")
            self.check_binding(msg)
            if msg["method"] == "thread/closed" and msg["params"].get("threadId") != self.thread:
                raise ProtocolError("wrong Codex shutdown thread")
        raise ProtocolError("Codex shutdown record limit exceeded")

    async def start(self):
        # Start confirmations come from the response. Opt out of the redundant
        # notifications, which can arrive on either side of that response.
        await self.rpc("initialize", {"clientInfo": {"name": "agent_office", "version": "0.1.0"},
                                      "capabilities": {"optOutNotificationMethods": [
                                          "thread/started", "turn/started", *sorted(INFORMATIONAL)]}})
        await self.send({"method": "initialized", "params": {}})
        response = await self.rpc("thread/start", {
            "cwd": str(self.workspace), "sandbox": "workspace-write" if self.edits else "read-only",
            "approvalPolicy": "on-request", "approvalsReviewer": "user", "ephemeral": True,
            "config": {"sandbox_workspace_write": {"writable_roots": [], "network_access": False,
                       "exclude_slash_tmp": True, "exclude_tmpdir_env_var": True}},
        })
        self.check_sandbox(response)
        self.thread = _identifier(_object(response.get("thread")).get("id"))

    async def permission(self, msg, active, files, receipts, denied_commands):
        method, params, rpc_id = msg["method"], msg["params"], msg["id"]
        if type(rpc_id) not in (int, str) or rpc_id in self.server_ids:
            raise ProtocolError("invalid or reused Codex server request ID")
        self.server_ids.add(rpc_id)
        if params.get("threadId") != self.thread or params.get("turnId") != self.turn:
            raise ProtocolError("unbound Codex server request")
        if method == "item/commandExecution/requestApproval":
            item_id = _identifier(params.get("itemId"))
            if active.get(item_id) != "commandExecution":
                raise ProtocolError("Codex command approval has no open command")
            denied_commands.add(item_id)
            result = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "item/tool/requestUserInput":
            # This API can carry an app's side-effect approval as well as a
            # technical query. Codex treats a missing approval answer as Abort.
            result = {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        elif method == "item/fileChange/requestApproval":
            item_id = _identifier(params.get("itemId"))
            file = files.get(item_id)
            if file is None or file["action"] is not None:
                raise ProtocolError("Codex patch approval has no unique open item")
            reason = params.get("reason")
            can_offer = (self.edits and params.get("grantRoot") is None
                         and set(params) <= {"threadId", "turnId", "itemId", "reason", "grantRoot", "startedAtMs"}
                         and (reason is None or isinstance(reason, str) and snapshot_request(reason) == reason)
                         and _changes(self.workspace, file["changes"]))
            action, decision_id = "decline", None
            if can_offer:
                details = {"worktree": str(self.workspace), "changes": file["changes"], "reason": reason}
                preview = json.dumps(details, ensure_ascii=False, indent=2)
                row = await self.ask("permission", preview)
                if row.get("decision") not in ("approve", "reject"):
                    raise ProtocolError("invalid Codex patch decision")
                if not _changes(self.workspace, file["changes"]):
                    raise ProtocolError("Codex patch target changed while awaiting approval")
                action = "accept" if row["decision"] == "approve" else "decline"
                decision_id = row["id"]
            file.update(action=action, decision=decision_id, resolved=False)
            receipts[rpc_id] = item_id
            await self.send({"id": rpc_id, "result": {"decision": action}}, decision_id)
            return
        else:
            raise ProtocolError("unsupported Codex server request")
        receipts[rpc_id] = None
        await self.send({"id": rpc_id, "result": result})

    async def complete_turn(self):
        files, receipts, active, finished = {}, {}, {}, set()
        denied_commands = set()
        answer = None
        while True:
            msg = await self.event()
            if "method" not in msg:
                raise ProtocolError("unexpected Codex response")
            method, params = msg["method"], msg["params"]
            if "id" in msg:
                await self.permission(msg, active, files, receipts, denied_commands)
                continue
            if params.get("threadId") != self.thread:
                raise ProtocolError("unbound Codex notification")
            if method == "serverRequest/resolved":
                key = params.get("requestId")
                if type(key) not in (int, str) or key not in receipts:
                    raise ProtocolError("unknown Codex request receipt")
                item_id = receipts.pop(key)
                if item_id is not None:
                    files[item_id]["resolved"] = True
                continue
            if method == "turn/completed":
                turn = _object(params.get("turn"))
                if (turn.get("id") != self.turn or turn.get("status") != "completed"
                        or turn.get("error") is not None or receipts or files or active or not answer):
                    raise ProtocolError("Codex did not complete the expected turn")
                return answer
            if params.get("turnId") != self.turn:
                raise ProtocolError("unbound Codex item")
            if method == "item/fileChange/patchUpdated":
                item_id = _identifier(params.get("itemId"))
                if item_id not in active and item_id not in finished:
                    # Optional streaming emits incomplete model output before
                    # the native fileChange item. It cannot authorize a patch.
                    continue
                file = files.get(item_id)
                if file is None or params.get("changes") != file["changes"]:
                    raise ProtocolError("Codex changed the authoritative patch")
                continue
            if method not in ("item/started", "item/completed"):
                raise ProtocolError("unexpected Codex turn event")
            item = _object(params.get("item"))
            item_id, kind = _identifier(item.get("id")), _identifier(item.get("type"))
            if method == "item/started":
                if item_id in active or item_id in finished or answer:
                    raise ProtocolError("unexpected Codex item start")
                active[item_id] = kind
                if kind == "fileChange":
                    if item.get("status") != "inProgress":
                        raise ProtocolError("invalid pending Codex file change")
                    files[item_id] = {"changes": item.get("changes"), "action": None}
                continue
            if active.pop(item_id, None) != kind:
                raise ProtocolError("unexpected Codex item completion")
            finished.add(item_id)
            if item_id in denied_commands and item.get("status") != "declined":
                raise ProtocolError("Codex did not confirm command refusal")
            if kind == "fileChange":
                file = files.pop(item_id)
                status = item.get("status")
                if status not in ("completed", "declined") or not self.edits and status == "completed":
                    raise ProtocolError("Codex file change did not complete in the permitted mode")
                if file["action"] is not None:
                    if (not file["resolved"] or (file["action"] == "accept") != (status == "completed")
                            or file["changes"] != item.get("changes")):
                        raise ProtocolError("Codex did not confirm the exact patch decision")
                    if file["decision"]:
                        self.inbox.delivered(file["decision"])
            elif (kind == "agentMessage" and item.get("delivery") != "async"
                  and item.get("phase") in (None, "final_answer")):
                value = item.get("text")
                if not isinstance(value, str) or not value.strip() or item.get("delivery") or item.get("questions"):
                    raise ProtocolError("invalid Codex final message")
                # A phase-less completed item requires our explicit turn marker.
                lines = value.rstrip().splitlines()
                if (item.get("phase") == "final_answer" or lines[0] in (QUESTION_MARKER, PRODUCT_DECISION_MARKER)
                        or lines[-1] == FINAL_MARKER):
                    if answer is not None:
                        raise ProtocolError("multiple final Codex messages")
                    answer = value

    async def run(self, prompt):
        await self.start()
        finish_instruction = "For the completed task append a final standalone line " + FINAL_MARKER + "."
        prompt += "\n\n" + QUESTION_INSTRUCTIONS + " " + finish_instruction
        decision = kind = direction = None
        questions = 0
        while True:
            try:
                response = await self.rpc("turn/start", {"threadId": self.thread, "input": [{"type": "text", "text": prompt}]},
                                          decision, direction=direction)
            except DelegationWithdrawn:
                row = await self.wait_decision(self.inbox.await_operator(decision))
                response = await self.rpc("turn/start", {"threadId": self.thread,
                                          "input": [{"type": "text", "text": continuation_prompt(row["answer"], kind=kind)}]},
                                          decision)
            turn = _object(response.get("turn"))
            self.turn = _identifier(turn.get("id"))
            if self.turn in self.turn_ids or turn.get("status") != "inProgress":
                raise ProtocolError("Codex did not start a distinct active turn")
            self.turn_ids.add(self.turn)
            for early, _ in self.early:
                self.check_binding(early)
            if decision:
                self.inbox.delivered(decision)
            if direction is not None:
                direction.delivered()
            message = await self.complete_turn()
            request = extract_request(message)
            if request is not None:
                if questions == MAX_QUESTIONS:
                    raise ProtocolError("Codex question limit reached")
                questions += 1
                kind, question = request
                row = await self.ask(kind, question)
                if row.get("decision") != "answer" or not isinstance(row.get("answer"), str) or not row["answer"].strip():
                    raise ProtocolError("invalid Codex clarification")
                decision, direction = row["id"], None
                prompt = continuation_prompt(row["answer"], source=row.get("source"), kind=kind)
                continue
            lines = message.rstrip().splitlines()
            if lines[-1] == FINAL_MARKER:
                lines.pop()
            final = "\n".join(lines).rstrip()
            if not final or FINAL_MARKER in lines:
                raise ProtocolError("invalid final Codex answer")
            direction = self.directions.next(available=not self.early) if self.directions is not None else None
            if direction is None:
                return AgentResult(0, final[:12000], self.thread, len(final) > 12000)
            decision = None
            prompt = direction.prompt + "\n\n" + QUESTION_INSTRUCTIONS + " " + finish_instruction


async def _run(executable, prompt, workspace, edits, env, timeout, inbox):
    deadline = asyncio.get_running_loop().time() + timeout
    async with asyncio.timeout_at(deadline):
        process = await asyncio.create_subprocess_exec(
            executable, "app-server", "--listen", "stdio://", cwd=workspace, env=env,
            start_new_session=os.name != "nt", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024)
        try:
            session = CodexSession(process, workspace.resolve(), edits, inbox, deadline)
            result = await session.run(prompt)
            await session.finish()
            return result
        finally:
            await close_stdio_process(process)


def run_codex_inbox(executable, prompt, workspace, allow_edits, env, timeout, inbox):
    try:
        return asyncio.run(_run(executable, prompt, workspace, allow_edits, env, timeout, inbox))
    except (TimeoutError, OfficeError):
        return AgentResult(124, "Codex timed out or the Studio request closed.")
    except (ValueError, RecursionError, OSError):
        return AgentResult(1, "Codex did not complete the expected app-server protocol.")

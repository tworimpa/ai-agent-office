"""Gemini and Cline ACP v1 with one isolated local session per process."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import nullcontext
from pathlib import Path

from .dialogue import DelegationWithdrawn, FINAL_MARKER, MAX_QUESTIONS, QUESTION_INSTRUCTIONS, continuation_prompt, extract_request
from .engines import CLINE_ACP_VERSION, cline_compatible
from .process_diagnostics import ErrorTail, close_stdio_process, drain_async
from .events import snapshot_request


class ProtocolError(ValueError):
    pass


def _unique_fields(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ProtocolError("duplicate JSON field")
        record[key] = value
    return record


def _file_target(workspace: Path, value: object, *, writing: bool) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = Path(value)
    if not path.is_absolute() or not path.is_relative_to(workspace):
        return False
    relative = path.relative_to(workspace)
    if not relative.parts:
        return False
    # Reject aliases, Git metadata, alternate streams and Windows device names.
    if any(part in (".", "..") or part.lower() == ".git" or ":" in part
           or part.endswith((".", " ")) for part in relative.parts):
        return False
    try:
        is_reserved = getattr(os.path, "isreserved", None)
        if (any(is_reserved(part) if is_reserved else Path(part).is_reserved() for part in relative.parts)
                or path.resolve() != path):
            return False
        if path.exists():
            return path.is_file() and path.stat().st_nlink == 1
        return writing
    except (OSError, RuntimeError, ValueError):
        return False


def _line(value: object) -> bool:
    return value is None or type(value) is int and value > 0


def _permitted(call: dict, workspace: Path, allow_edits: bool) -> bool:
    # Cline generates kind from an exact tool-name table; the generated title
    # prefixes the original name. Never authorize an MCP, shell or generic edit.
    name = str(call.get("title", "")).partition(": ")[0]
    data = call.get("rawInput")
    if not isinstance(data, dict):
        return False
    if (name, call.get("kind")) == ("read_files", "read") and set(data) == {"files"}:
        files = data["files"]
        return isinstance(files, list) and bool(files) and all(
            isinstance(f, dict) and set(f) <= {"path", "start_line", "end_line"}
            and _line(f.get("start_line")) and _line(f.get("end_line"))
            and _file_target(workspace, f.get("path"), writing=False) for f in files
        )
    if allow_edits and (name, call.get("kind")) == ("editor", "edit"):
        return (set(data) <= {"path", "old_text", "new_text", "insert_line"}
                and isinstance(data.get("new_text"), str)
                and (data.get("old_text") is None or isinstance(data["old_text"], str))
                and _line(data.get("insert_line"))
                and _file_target(workspace, data.get("path"), writing=True))
    return False


def _describe_call(call: dict) -> str:
    name = call["title"].partition(": ")[0]
    data = call["rawInput"]
    # Check original strings first: JSON quoting can conceal an assignment
    # such as an embedded JSON password field from the journal's redactor.
    values = (f["path"] for f in data["files"]) if name == "read_files" else data.values()
    if any(isinstance(value, str) and snapshot_request(value) != value for value in values):
        raise ProtocolError("Cline permission input cannot be recorded in full")
    return name + "\n" + json.dumps(data, ensure_ascii=False, indent=2)


async def write_record(process, record, *, inbox, decision_id, deadline, direction=None):
    guard = nullcontext()
    if decision_id is not None:
        guard = inbox.sending(decision_id)
    elif direction is not None:
        guard = direction.sending()
    with guard:
        # Acquiring the journal writer lock can outlast the asyncio timer.
        # A synchronous write must independently honor the original deadline.
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        process.stdin.write(record)
    await process.stdin.drain()


class _Session:
    def __init__(self, engine, process, workspace, allow_edits, strict_completion, inbox, timer, *, prompt_only=False, accepts_version=None, telemetry=None, progress=None):
        self.engine = engine
        self.process = process
        self.workspace = workspace
        self.strict_completion = strict_completion
        self.prompt_only = prompt_only
        self.accepts_version = accepts_version or (lambda version: version == CLINE_ACP_VERSION)
        self.telemetry = telemetry
        self.progress = progress
        self.inbox = inbox
        self.directions = getattr(inbox, "directions", None)
        self.timer = timer
        self.gemini_calls = {}
        self.file_operations = set()
        self.approved_operations = {}
        self.allow_edits = allow_edits
        self.session_id = ""
        self.mode = ("act" if engine == "cline" else "autoEdit") if allow_edits else "plan"
        self.fragments = []
        self.pending = set()
        self.granted = set()
        self.rejected = set()
        self.requested_calls = {}
        self.permission_receipts = {}
        self.permission_request_ids = set()
        self.output_chars = 0
        self.records = 0
        self.retry_waiting = False

    async def send(self, *, decision_id=None, direction=None, **message):
        encoded = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False)
        record = (encoded + "\n").encode("utf-8")
        await write_record(self.process, record, inbox=self.inbox, decision_id=decision_id, deadline=self.timer.when(),
                           direction=direction)

    async def request(self, request_id, method, *, decision_id=None, direction=None, **params):
        await self.send(id=request_id, method=method, params=params, decision_id=decision_id, direction=direction)
        while True:
            self.records += 1
            if self.records > 50_000:
                raise ProtocolError("ACP record count exceeded limit")
            line = await self.process.stdout.readline()
            if not line:
                raise ProtocolError("EOF before the ACP response")
            msg = json.loads(line, object_pairs_hook=_unique_fields)
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                raise ProtocolError("invalid ACP record")
            if "method" not in msg:
                if type(msg.get("id")) is not int or msg["id"] != request_id:
                    raise ProtocolError("unexpected ACP response ID")
                if "error" in msg:
                    error = msg["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    detail = f" (RPC {code})" if type(code) is int else ""
                    raise ProtocolError(f"{self.engine} rejected {method}{detail}")
                if not isinstance(msg.get("result"), dict):
                    raise ProtocolError("invalid ACP result")
                if decision_id is not None:
                    self.inbox.delivered(decision_id)
                return msg["result"]
            data = msg.get("params")
            if (not self.session_id or not isinstance(data, dict)
                    or data.get("sessionId") != self.session_id):
                raise ProtocolError("unexpected ACP session")
            if msg["method"] == "session/request_permission" and "id" in msg and method == "session/prompt":
                await self.permission(msg["id"], data)
            elif msg["method"] == "session/update" and "id" not in msg:
                self.update(data.get("update"), request_id=request_id)
            else:
                raise ProtocolError("unsupported ACP client operation")

    async def ask(self, kind, prompt):
        return await self.wait_decision(self.inbox.ask(kind, prompt, self.session_id))

    async def wait_decision(self, awaitable):
        from .dialogue import operator_wait
        with operator_wait(self.inbox, self.timer):
            return await self._live_decision(awaitable)

    async def _live_decision(self, awaitable):
        # A dead provider cannot consume a later decision. Race its exit even
        # while the normal stdout reader is waiting for the operator.
        response = asyncio.create_task(awaitable)
        exited = asyncio.create_task(self.process.wait())
        try:
            await asyncio.wait((response, exited), return_when=asyncio.FIRST_COMPLETED)
            if exited.done() or self.process.returncode is not None:
                raise ProtocolError("ACP session ended while waiting for the operator")
            return response.result()
        finally:
            for task in (response, exited):
                task.cancel()
            await asyncio.gather(response, exited, return_exceptions=True)

    async def permission(self, request_id, data):
        if type(request_id) not in (int, str) or request_id in self.permission_request_ids:
            raise ProtocolError("invalid permission request ID")
        self.permission_request_ids.add(request_id)
        if self.prompt_only:
            await self.send(id=request_id, result={"outcome": {"outcome": "cancelled"}})
            raise ProtocolError("Meeting consultation requested tool permission")
        if self.engine == "gemini":
            await self.send(id=request_id, result={"outcome": {"outcome": "cancelled"}})
            raise ProtocolError("Gemini requested interactive permission")
        call = data.get("toolCall")
        if not isinstance(call, dict) or not isinstance(data.get("options"), list):
            raise ProtocolError("invalid ACP permission request")
        tool_id = call.get("toolCallId")
        if not isinstance(tool_id, str) or not tool_id or tool_id in self.granted or tool_id in self.rejected:
            raise ProtocolError("invalid or reused tool ID")
        allow = _permitted(call, self.workspace, self.allow_edits)

        def offered(kind):
            matches = [o for o in data["options"] if isinstance(o, dict) and o.get("optionId") == kind]
            return len(matches) == 1 and matches[0].get("kind") == kind

        if not allow or not offered("allow_once") or (self.inbox is not None and not offered("reject_once")):
            outcome = {"outcome": "selected", "optionId": "reject_once"} if offered("reject_once") else {"outcome": "cancelled"}
            await self.send(id=request_id, result={"outcome": outcome})
            raise ProtocolError("Cline requested a tool outside this role's workspace file permissions")
        kind = "allow_once"
        decision_id = None
        if self.inbox is not None:
            row = await self.ask("permission", _describe_call(call))
            if row["decision"] not in ("approve", "reject"):
                raise ProtocolError("invalid tool permission decision")
            if row["decision"] == "approve" and not _permitted(call, self.workspace, self.allow_edits):
                raise ProtocolError("permission target changed while waiting for the operator")
            kind = "allow_once" if row["decision"] == "approve" else "reject_once"
            decision_id = row["id"]
            self.permission_receipts[tool_id] = decision_id
            self.requested_calls[tool_id] = (call["title"], call["kind"], call["rawInput"])
        await self.send(id=request_id, result={"outcome": {"outcome": "selected", "optionId": kind}},
                        decision_id=decision_id)
        self.pending.add(tool_id)
        if kind == "allow_once":
            self.granted.add(tool_id)
            self.approved_operations[tool_id] = call["kind"]
        else:
            self.rejected.add(tool_id)
        self.fragments.clear()

    def update(self, update, *, request_id):
        if not isinstance(update, dict):
            raise ProtocolError("invalid ACP update")
        kind = update.get("sessionUpdate")
        if kind == "current_mode_update" and update.get("currentModeId") == self.mode:
            return
        if kind == "usage_update" and self.telemetry and request_id >= 4:
            from .provider_telemetry import bounded_int
            used, size = update.get("used"), update.get("size")
            if bounded_int(used) and bounded_int(size) and 0 < size and used <= size:
                self.telemetry({"kind":"usage", "context_used":used, "context_window":size})
        if kind in ("session_info_update", "available_commands_update", "usage_update"):
            return
        content = update.get("content")
        if (self.engine == "gemini" and kind == "agent_message_chunk" and isinstance(content, dict)
                and content.get("type") == "text" and isinstance(content.get("text"), str)
                and content["text"].startswith("[MODE_UPDATE] ")):
            if request_id == 3 and content["text"] == f"[MODE_UPDATE] {self.mode}":
                return
            raise ProtocolError("unexpected Gemini mode change")
        if request_id < 4:
            raise ProtocolError("unexpected ACP setup notification")
        meta = update.get("_meta", {})
        if not isinstance(meta, dict):
            raise ProtocolError("invalid ACP update metadata")
        if self.engine == "gemini" and ("retry" in meta or "reset" in meta):
            if self.pending or meta.get("retry") is not True:
                raise ProtocolError("ambiguous Gemini retry")
            if kind == "agent_thought_chunk" and "reset" not in meta:
                self.retry_waiting = True
            elif kind == "agent_message_chunk" and meta.get("reset") is True:
                self.retry_waiting = False
            else:
                raise ProtocolError("unsupported Gemini retry notification")
            self.fragments.clear()
        if kind == "agent_thought_chunk":
            return
        if kind == "agent_message_chunk":
            if self.retry_waiting:
                raise ProtocolError("missing Gemini stream reset")
            content = update.get("content")
            if not isinstance(content, dict) or content.get("type") != "text" or not isinstance(content.get("text"), str):
                raise ProtocolError("unsupported ACP message")
            self.output_chars += len(content["text"])
            if self.output_chars > 1_000_000:
                raise ProtocolError("ACP response exceeded output limit")
            self.fragments.append(content["text"])
            if self.progress:
                self.progress("response")
            return
        if kind in ("tool_call", "tool_call_update"):
            if self.prompt_only:
                raise ProtocolError("Meeting consultation reported a tool call")
            if self.progress:
                self.progress("tool_finished" if update.get("status") == "completed" else "tool")
            tool_id = update.get("toolCallId")
            status = update.get("status")
            if not isinstance(tool_id, str) or not tool_id:
                raise ProtocolError("invalid tool notification")
            if self.engine == "gemini":
                self.gemini_tool(update, kind, tool_id, status)
                return
            if status == "pending":
                # Cline emits tool-started for its result wrapper even when the
                # handler was skipped after rejection. Require the same input
                # and a subsequent failed update; never grant another call.
                if tool_id in self.requested_calls:
                    identity = (update.get("title"), update.get("kind"), update.get("rawInput"))
                    if identity != self.requested_calls[tool_id]:
                        raise ProtocolError("Cline changed the requested operation")
                    if tool_id in self.granted and tool_id not in self.pending:
                        raise ProtocolError("Cline reused a completed tool ID")
                self.pending.add(tool_id)
            elif tool_id in self.rejected:
                if status != "failed" or kind != "tool_call_update":
                    raise ProtocolError("Cline did not honor the rejected tool operation")
                self.pending.discard(tool_id)
                self.fragments.clear()
            elif status == "in_progress" and tool_id in self.granted and tool_id in self.pending:
                pass
            elif status == "completed" and tool_id in self.granted and tool_id in self.pending:
                output = update.get("rawOutput")
                results = output if isinstance(output, list) else [output]
                if not results or not all(isinstance(r, dict) and r.get("success") is True
                                          and not r.get("error") for r in results):
                    raise ProtocolError("Cline file operation did not report success")
                self.pending.remove(tool_id)
                self.file_operations.add(self.approved_operations[tool_id])
                self.fragments.clear()
            else:
                raise ProtocolError("failed or unapproved Cline tool operation")
            if status != "pending" and tool_id in self.permission_receipts:
                self.inbox.delivered(self.permission_receipts.pop(tool_id))
            return
        raise ProtocolError("unsupported ACP session update")

    def gemini_tool(self, update, event_kind, tool_id, status):
        if event_kind == "tool_call" and status == "pending":
            self.pending.add(tool_id)
            return
        allowed = {"read", "search"} | ({"edit"} if self.allow_edits else set())
        operation = update.get("kind")
        if not isinstance(operation, str) or operation not in allowed:
            raise ProtocolError("Gemini reported a tool outside the assigned role")
        if event_kind == "tool_call" and status == "in_progress" and tool_id not in self.gemini_calls:
            self.gemini_calls[tool_id] = operation
            self.pending.add(tool_id)
        elif (event_kind == "tool_call_update" and status == "completed" and tool_id in self.pending
              and self.gemini_calls.get(tool_id) == operation):
            self.pending.remove(tool_id)
            # The upstream ACP search kind also includes web search. Only the
            # completed file tools report absolute file locations in this release.
            locations = update.get("locations")
            if (operation in {"read", "edit"} and isinstance(locations, list) and locations
                    and all(isinstance(loc, dict) and _file_target(self.workspace, loc.get("path"),
                            writing=False) for loc in locations)):
                self.file_operations.add(operation)
        else:
            raise ProtocolError("Gemini reported a failed or incomplete tool operation")
        self.fragments.clear()

    async def run(self, prompt):
        init = await self.request(1, "initialize", protocolVersion=1, clientCapabilities={})
        if type(init.get("protocolVersion")) is not int or init["protocolVersion"] != 1:
            raise ProtocolError("unsupported ACP version")
        info = init.get("agentInfo")
        if self.engine == "cline" and (not isinstance(info, dict) or info.get("name") != "cline"
                                       or not cline_compatible(info.get("version"))
                                       or not self.accepts_version(info["version"])):
            raise ProtocolError("Cline version requires a host-specific verify-engine capability check")
        new = await self.request(2, "session/new", cwd=str(self.workspace), mcpServers=[])
        self.session_id = new.get("sessionId")
        options = new.get("configOptions")
        if not isinstance(self.session_id, str) or not 0 < len(self.session_id) <= 256:
            raise ProtocolError("missing ACP session ID")
        if self.engine == "cline":
            controls = ([o for o in options if isinstance(o, dict) and o.get("id") == "auto_approve"]
                        if isinstance(options, list) else [])
            if len(controls) != 1 or controls[0].get("currentValue") is not False:
                raise ProtocolError("Cline did not confirm an approval-controlled session")
        if self.engine == "gemini" or info.get("version") != CLINE_ACP_VERSION:
            modes = new.get("modes")
            if not isinstance(modes, dict) or not isinstance(modes.get("availableModes"), list):
                raise ProtocolError(f"{self.engine} did not advertise session modes")
            if not any(isinstance(m, dict) and m.get("id") == self.mode for m in modes["availableModes"]):
                raise ProtocolError(f"{self.engine} does not support the requested session mode")
        await self.request(3, "session/set_mode", sessionId=self.session_id, modeId=self.mode)
        if self.prompt_only:
            instructions = "Use only the supplied project excerpts. Do not use tools."
        elif self.engine == "cline":
            instructions = "Use read_files with absolute workspace paths for reads."
            if self.allow_edits:
                instructions += " Use editor for file edits."
            instructions += " Only those file operations can receive permission."
        else:
            instructions = "Use workspace file reads and searches."
            if self.allow_edits:
                instructions += " Use file edit tools for changes."
        instructions += " After completing the task, append a final line containing exactly " + FINAL_MARKER + "."
        if self.inbox is not None:
            instructions += " " + QUESTION_INSTRUCTIONS
        if self.progress:
            self.progress("session")
        turn_text = prompt + "\n\n" + instructions
        decision_id = kind = direction = None
        questions, request_id = 0, 4
        while True:
            self.fragments.clear()
            try:
                end = await self.request(request_id, "session/prompt", sessionId=self.session_id,
                                         prompt=[{"type": "text", "text": turn_text}], decision_id=decision_id,
                                         direction=direction)
            except DelegationWithdrawn:
                row = await self.wait_decision(self.inbox.await_operator(decision_id))
                end = await self.request(request_id, "session/prompt", sessionId=self.session_id,
                                         prompt=[{"type": "text", "text": continuation_prompt(row["answer"], kind=kind)}],
                                         decision_id=decision_id)
            if end.get("stopReason") != "end_turn" or self.pending or self.retry_waiting:
                raise ProtocolError("ACP did not complete its turn")
            if direction is not None:
                direction.delivered()
            request_id += 1
            text = "".join(self.fragments)
            request = extract_request(text) if self.inbox is not None else None
            if request is not None:
                if questions == MAX_QUESTIONS:
                    raise ProtocolError("ACP question limit reached")
                questions += 1
                kind, question = request
                row = await self.ask(kind, question)
                if row["decision"] != "answer" or not isinstance(row["answer"], str) or not row["answer"].strip():
                    raise ProtocolError("invalid operator answer")
                decision_id, direction = row["id"], None
                turn_text = continuation_prompt(row["answer"], source=row.get("source"), kind=kind)
                continue
            answer, sep, marker = text.rstrip().rpartition("\n")
            if not sep or marker.strip() != FINAL_MARKER or not answer.strip() or FINAL_MARKER in answer.splitlines():
                raise ProtocolError("ACP did not return a complete final answer")
            direction = self.directions.next() if self.directions is not None else None
            if direction is None:
                break
            if direction.meeting_seconds:
                self.timer.reschedule(self.timer.when() + direction.meeting_seconds)
            decision_id = None
            turn_text = direction.prompt + "\n\n" + instructions
        if self.strict_completion and not self.prompt_only:
            required = {"read", "edit"} if self.allow_edits else {"read"}
            if not required.issubset(self.file_operations):
                raise ProtocolError(f"{self.engine} did not report the expected workspace file operations")
        if self.telemetry:
            from .provider_telemetry import acp_usage
            if receipt := acp_usage(end.get("usage")):
                self.telemetry(receipt)
        return answer, self.session_id


async def run_acp(engine, argv, prompt, workspace, allow_edits, env, timeout, strict_completion, inbox=None, *,
                  prompt_only=False, accepts_version=None, telemetry=None, progress=None):
    if engine == "cline":
        env = {**env, "CLINE_SESSION_BACKEND_MODE": "local"}
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=workspace, env=env, start_new_session=os.name != "nt",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024,
    )
    tail = ErrorTail()
    reader = asyncio.create_task(drain_async(process.stderr, tail.append))
    try:
        async with asyncio.timeout(timeout) as timer:
            session = _Session(engine, process, workspace.resolve(), allow_edits, strict_completion, inbox, timer,
                               prompt_only=prompt_only, accepts_version=accepts_version, telemetry=telemetry, progress=progress)
            result = await session.run(prompt)
            process.stdin.close()
            # Reject any second terminal result or trailing protocol output.
            if (await process.stdout.read(1)) or await process.wait() != 0:
                raise ProtocolError("ACP server exited unsuccessfully or sent trailing data")
            return result
    except (ValueError, OSError) as exc:
        await close_stdio_process(process)
        try:
            await asyncio.wait_for(asyncio.shield(reader), timeout=1)
        except TimeoutError:
            pass
        cause = tail.description()
        if cause and not isinstance(exc, TimeoutError):
            raise ProtocolError(f"{exc} ({cause})") from exc
        raise
    finally:
        await close_stdio_process(process)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

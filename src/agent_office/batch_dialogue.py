"""Ask technical questions between completed CLI turns, resuming only the named session."""
import asyncio
from contextlib import nullcontext
import json
import os
import subprocess
import tempfile
import threading
import time
from uuid import UUID

from .adapters import AgentResult, parse_antigravity, parse_output, record_exec_usage
from .dialogue import DelegationWithdrawn, MAX_QUESTIONS, QUESTION_INSTRUCTIONS, continuation_prompt, extract_request
from .process_diagnostics import ErrorTail, drain_pipe, kill_family
from .provider_telemetry import batch_receipt
from .fixture_completion import parse_fixture_completion


def _resume_argv(engine, original, session):
    # Never let a provider interpret a session name as a path, flag or "most recent".
    parsed = UUID(session)
    if session.lower() not in (str(parsed), parsed.hex):
        raise ValueError("The provider did not supply an unambiguous conversation UUID.")
    if engine == "claude":
        return [*original, "--resume", session]
    if engine == "antigravity":
        return [*original, "--conversation", session]
    raise ValueError("This engine has no supported continuation command.")


def _turn(engine, argv, message, workspace, env, deadline, guard, *, telemetry=None, progress=None, strict=True):
    if engine == "antigravity":
        message = json.dumps({"event": "user", "message": {"content": message}}, ensure_ascii=False) + "\n"
    if engine == "claude" and progress and "--output-format" in argv:
        argv = list(argv)
        argv[argv.index("--output-format") + 1] = "stream-json"
        argv.append("--verbose")
    tail = ErrorTail()
    raw = bytearray()
    line_start = 0

    def output_block(block):
        nonlocal line_start
        remaining = 4_000_001 - len(raw)
        if remaining > 0:
            raw.extend(block[:remaining])
        if progress:
            while (end := raw.find(b"\n", line_start)) >= 0:
                line = raw[line_start:end] if end - line_start <= 65536 else b""
                line_start = end + 1
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(record, dict):
                    kind = record.get("event") if engine == "antigravity" else record.get("type")
                    activity = {"init":"session", "thread.started":"session",
                                "assistant":"response", "tool_progress":"tool"}.get(kind) if isinstance(kind,str) else None
                    if kind == "step_update" and isinstance(record.get("step_update"), dict):
                        activity = "tool" if isinstance(record["step_update"].get("tool_info"), dict) else "output"
                    if kind in ("item.started", "item.completed") and isinstance(record.get("item"), dict):
                        operation = record["item"].get("type")
                        if operation == "agent_message":
                            activity = "response"
                        elif operation in ("command_execution", "file_change", "mcp_tool_call"):
                            activity = "tool_finished" if kind == "item.completed" else (
                                "command" if operation == "command_execution" else "file" if operation == "file_change" else "tool")
                    if activity:
                        progress(activity)

    with tempfile.TemporaryFile() as stdin:
        stdin.write(message.encode("utf-8"))
        stdin.seek(0)
        with guard:
            if time.monotonic() >= deadline:
                raise TimeoutError("The original task timed out before the continuation could start.")
            proc = subprocess.Popen(argv, cwd=workspace, stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, start_new_session=os.name != "nt")
        finished = threading.Event()
        readers = [drain_pipe(proc.stderr, tail.append, finished=finished),
                   drain_pipe(proc.stdout, output_block, finished=finished)]
        try:
            if progress:
                progress("started")
            code = proc.wait(timeout=max(0, deadline - time.monotonic()))
        finally:
            if proc.poll() is None:
                kill_family(proc.pid)
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=4)
            finished.set()
            for reader in readers:
                reader.join(timeout=1)
        failure = None
        if code:
            cause = tail.description()
            failure = AgentResult(code, f"{engine} exited {code}" + (f" ({cause})" if cause else ""))
        if any(reader.is_alive() for reader in readers):
            if os.name != "nt":
                kill_family(proc.pid)
            raise ValueError("Provider descendants retained the output pipe.")
    if len(raw) > 4_000_000:
        if failure:
            return failure
        raise ValueError("Inbox turn exceeded the output limit.")
    output = raw.decode("utf-8", errors="replace")
    if engine == "claude" and progress:
        from .provider_telemetry import claude_stream
        try:
            output, limits = claude_stream(output)
        except (ValueError, RecursionError):
            if failure:
                return failure
            raise
        if telemetry:
            for limit in limits:
                telemetry(limit)
    if telemetry and engine in ("claude", "antigravity"):
        for receipt in batch_receipt(engine, output):
            telemetry(receipt)
    if failure:
        return failure
    if engine == "codex" and telemetry:
        record_exec_usage(output, telemetry)
    if engine == "antigravity":
        return parse_antigravity(output)
    if not strict:
        message, session = parse_output(engine, output)
        return AgentResult(0, message[:12000], session, len(message) > 12000)
    result = parse_fixture_completion(engine, output)
    if result.returncode:
        return result
    _, session_id = parse_output(engine, output)
    return AgentResult(0, result.message, session_id, result.truncated)


def run_batch_dialogue(engine, argv, prompt, workspace, env, timeout, inbox, *, progress=None, telemetry=None):
    deadline = time.monotonic() + timeout

    def wait_decision(awaitable):
        nonlocal deadline
        started = time.monotonic()
        if getattr(inbox, "goal_task_id", None) is not None and started < deadline:
            # The previous batch turn has exited. Only Studio's live request/stop guard
            # is needed until the operator answers on the original session.
            response = asyncio.run(awaitable)
            deadline += time.monotonic() - started
            return response
        return asyncio.run(asyncio.wait_for(awaitable, timeout=max(0, deadline - started)))
    message = prompt + "\n\n" + QUESTION_INSTRUCTIONS
    directions = getattr(inbox, "directions", None)
    session = decision = direction = kind = None
    command = argv
    questions = 0
    receipts = []
    turns = 0

    def observe(event):
        if event["kind"] == "usage":
            receipts.append(event)
        elif telemetry:
            telemetry(event)

    def finish(result):
        # Resumed invocations may report cumulative session usage. Only a
        # single, unambiguous terminal receipt establishes this call's cost.
        if telemetry and turns == len(receipts) == 1:
            telemetry(receipts[0])
        return result
    while True:
        guard = nullcontext()
        if decision is not None:
            guard = inbox.sending(decision["id"])
        elif direction is not None:
            guard = direction.sending()
        try:
            result = _turn(engine, command, message, workspace, env, deadline, guard, progress=progress, telemetry=observe)
        except DelegationWithdrawn:
            decision = wait_decision(inbox.await_operator(decision["id"]))
            result = _turn(engine, command, continuation_prompt(decision["answer"], kind=kind), workspace, env, deadline,
                           inbox.sending(decision["id"]), progress=progress, telemetry=observe)
        turns += 1
        if result.returncode:
            return finish(result)
        if (decision is not None or direction is not None) and result.session_id != session:
            raise ValueError("The provider did not confirm the original conversation ID.")
        if decision is not None:
            inbox.delivered(decision["id"])
        if direction is not None:
            direction.delivered()
        request = extract_request(result.message)
        if request is not None:
            if result.truncated or not isinstance(result.session_id, str):
                raise ValueError("Incomplete question or missing conversation ID.")
            if questions == MAX_QUESTIONS:
                raise ValueError("Operator question limit exceeded.")
            questions += 1
            kind, question = request
            session = result.session_id
            command = _resume_argv(engine, argv, session)
            decision = wait_decision(inbox.ask(kind, question, session))
            message = continuation_prompt(decision["answer"], source=decision.get("source"), kind=kind)
            direction = None
            continue
        if directions is None:
            return finish(result)
        session = result.session_id
        command = None
        if not result.truncated and isinstance(session, str):
            try:
                command = _resume_argv(engine, argv, session)
            except ValueError:
                pass
        direction = directions.next(available=command is not None)
        if direction is None:
            return finish(result)
        deadline += direction.meeting_seconds
        decision = None
        message = direction.prompt + "\n\n" + QUESTION_INSTRUCTIONS

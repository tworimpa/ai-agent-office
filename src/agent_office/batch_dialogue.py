"""Ask technical questions between completed CLI turns, resuming only the named session."""
import asyncio
from contextlib import nullcontext
import json
import subprocess
import tempfile
import time
from uuid import UUID

from .adapters import AgentResult, parse_antigravity, parse_output
from .dialogue import DelegationWithdrawn, MAX_QUESTIONS, QUESTION_INSTRUCTIONS, continuation_prompt, extract_request
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


def _turn(engine, argv, message, workspace, env, deadline, guard):
    if engine == "antigravity":
        message = json.dumps({"event": "user", "message": {"content": message}}, ensure_ascii=False) + "\n"
    # The complete input is ready before launch. Only the launch itself needs to
    # be serialized with stop/expiry, never a pipe write or a vendor round trip.
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout:
        stdin.write(message.encode("utf-8"))
        stdin.seek(0)
        with guard:
            if time.monotonic() >= deadline:
                raise TimeoutError("The original task timed out before the continuation could start.")
            proc = subprocess.Popen(argv, cwd=workspace, stdin=stdin, stdout=stdout,
                                    stderr=subprocess.DEVNULL, env=env)
        try:
            code = proc.wait(timeout=max(0, deadline - time.monotonic()))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if code:
            return AgentResult(code, f"{engine} exited {code}")
        stdout.seek(0)
        raw = stdout.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("Inbox turn exceeded the output limit.")
    output = raw.decode("utf-8")
    if engine == "antigravity":
        return parse_antigravity(output)
    result = parse_fixture_completion(engine, output)
    if result.returncode:
        return result
    _, session_id = parse_output(engine, output)
    return AgentResult(0, result.message, session_id, result.truncated)


def run_batch_dialogue(engine, argv, prompt, workspace, env, timeout, inbox):
    deadline = time.monotonic() + timeout
    message = prompt + "\n\n" + QUESTION_INSTRUCTIONS
    directions = getattr(inbox, "directions", None)
    session = decision = direction = kind = None
    command = argv
    questions = 0
    while True:
        guard = nullcontext()
        if decision is not None:
            guard = inbox.sending(decision["id"])
        elif direction is not None:
            guard = direction.sending()
        try:
            result = _turn(engine, command, message, workspace, env, deadline, guard)
        except DelegationWithdrawn:
            decision = asyncio.run(asyncio.wait_for(inbox.await_operator(decision["id"]),
                                                    timeout=max(0, deadline - time.monotonic())))
            result = _turn(engine, command, continuation_prompt(decision["answer"], kind=kind), workspace, env, deadline,
                           inbox.sending(decision["id"]))
        if result.returncode:
            return result
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
            decision = asyncio.run(asyncio.wait_for(inbox.ask(kind, question, session),
                                                    timeout=max(0, deadline - time.monotonic())))
            message = continuation_prompt(decision["answer"], source=decision.get("source"), kind=kind)
            direction = None
            continue
        if directions is None:
            return result
        session = result.session_id
        command = None
        if not result.truncated and isinstance(session, str):
            try:
                command = _resume_argv(engine, argv, session)
            except ValueError:
                pass
        direction = directions.next(available=command is not None)
        if direction is None:
            return result
        deadline += direction.meeting_seconds
        decision = None
        message = direction.prompt + "\n\n" + QUESTION_INSTRUCTIONS

"""Provider-specific command adapters. Never invoke agent CLIs with shell=True."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from .acp import run_acp
from .worker_environment import coding_environment
from .engines import SUPPORTED_ENGINES, codex_backend_args, resolve_executable
from .provider_profiles import validate_selection
from .engine_updates import host_lease
from .provider_telemetry import exec_usage


@dataclass(frozen=True)
class AgentResult:
    returncode: int
    message: str
    session_id: str | None = None
    truncated: bool = False
    preflight_blocked: bool = False


class CLIAdapter:
    def __init__(self, engine: str, timeout: int = 900):
        if engine not in SUPPORTED_ENGINES:
            raise ValueError(f"Unsupported engine: {engine}")
        self.engine = engine
        self.db_path = None
        self.progress = None
        self.fixture_version = None
        self.timeout = timeout

    def command(self, prompt: str, *, allow_edits: bool, model=None, effort=None) -> list[str]:
        validate_selection(self.engine, model, effort)
        selection = ["--model", model] if model else []
        if effort:
            selection += (["-c", f"model_reasoning_effort={effort}"] if self.engine == "codex" else ["--effort", effort])
        if self.engine == "gemini":
            return ["gemini", "--acp", "--approval-mode", "auto_edit" if allow_edits else "plan", *selection]
        if self.engine == "cline":
            return ["cline", "--acp", "--auto-approve", "false"]
        if self.engine == "antigravity":
            return ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
                    "--mode", "accept-edits" if allow_edits else "plan", "--sandbox",
                    "--disable-slash-commands", "--print-timeout", f"{self.timeout}s", *selection]
        if self.engine == "claude":
            return [
                "claude", "-p", "--output-format", "json",
                "--permission-mode", "acceptEdits" if allow_edits else "plan",
                "--max-turns", "16", *selection,
            ]
        if self.engine != "codex":
            raise ValueError(f"No command configured for {self.engine}")
        return [
            "codex", "exec", "--json", "--sandbox",
            "workspace-write" if allow_edits else "read-only", *selection, "-",
        ]

    def execute(self, prompt: str, *, workspace: Path, allow_edits: bool,
                strict_completion: bool = False, inbox=None, prompt_only: bool = False,
                model=None, effort=None, telemetry=None) -> AgentResult:
        # Pass untrusted task text via stdin: safer for Windows .cmd launchers,
        # long prompts, and special characters than putting it in argv.
        if prompt_only and (allow_edits or inbox is not None):
            raise ValueError("Consultations require a read-only role without an inbox.")
        selection = {key: value for key, value in (("model", model), ("effort", effort)) if value is not None}
        argv = self.command(prompt, allow_edits=allow_edits, **selection)
        executable = resolve_executable(argv[0])
        if executable is None:
            return AgentResult(127, f"Missing CLI: {argv[0]}. Install and authenticate it first.")
        argv[0] = executable
        if self.engine == "codex":
            argv[1:1] = codex_backend_args()
        # Python-based tools launched on Windows otherwise inherit a legacy
        # console encoding even though the parent writes UTF-8 to stdin.
        # For Node-based CLIs this has no behavioral effect.
        try:
            with host_lease(), coding_environment(os.environ) as env:
                env.setdefault("PYTHONUTF8", "1")
                env.setdefault("PYTHONIOENCODING", "utf-8")
                task_input = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n" if self.engine == "antigravity" else prompt
                if self.engine in ("gemini", "cline"):
                    from .engine_evidence import cline_verified
                    from .engines import CURRENT_ROLE
                    capability = "edit" if allow_edits else "review" if CURRENT_ROLE.get() == "reviewer" else "read"
                    message, session = asyncio.run(run_acp(
                        self.engine, argv, prompt, workspace, allow_edits, env, self.timeout, strict_completion, inbox,
                        prompt_only=prompt_only, telemetry=telemetry, progress=self.progress,
                        accepts_version=lambda version: version == self.fixture_version if self.fixture_version else cline_verified(
                            self.db_path, executable, version, capability),
                    ))
                    return AgentResult(0, message[:12000], session, len(message) > 12000)
                if prompt_only:
                    # Windows launchers may leave a descendant holding a stdio pipe
                    # after exit. A bounded file-backed turn does not await its EOF.
                    from .batch_dialogue import _turn
                    return _turn(self.engine, argv, prompt, workspace, env, time.monotonic() + self.timeout, nullcontext(),
                                 telemetry=telemetry, progress=self.progress)
                if inbox is not None:
                    if self.engine == "codex":
                        from .codex_inbox import run_codex_inbox
                        return run_codex_inbox(executable, prompt, workspace, allow_edits, env, self.timeout, inbox,
                                               model=model, effort=effort, telemetry=telemetry, progress=self.progress)
                    from .batch_dialogue import run_batch_dialogue
                    return run_batch_dialogue(self.engine, argv, prompt, workspace, env, self.timeout, inbox,
                                              progress=self.progress, telemetry=telemetry)
                if self.progress:
                    from .batch_dialogue import _turn
                    return _turn(self.engine, argv, prompt, workspace, env, time.monotonic()+self.timeout, nullcontext(),
                                 telemetry=telemetry, progress=self.progress, strict=strict_completion)
                completed = subprocess.run(
                    argv, cwd=workspace, input=task_input, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=self.timeout,
                    check=False,
                )
        except (ValueError, RecursionError) as exc:
            return AgentResult(1, f"{self.engine} protocol failure: {exc}")
        except (subprocess.TimeoutExpired, TimeoutError):
            return AgentResult(124, f"{self.engine} timed out after {self.timeout} seconds")
        except OSError as exc:
            return AgentResult(126, f"Unable to start {self.engine}: {exc}")

        if self.engine == "codex" and telemetry:
            record_exec_usage(completed.stdout, telemetry)
        elif telemetry:
            from .provider_telemetry import batch_receipt
            for receipt in batch_receipt(self.engine, completed.stdout):
                telemetry(receipt)
        if strict_completion and self.engine in ("codex", "claude") and completed.returncode == 0:
            from .fixture_completion import parse_fixture_completion
            return parse_fixture_completion(self.engine, completed.stdout)
        if self.engine == "antigravity":
            parsed = parse_antigravity(completed.stdout)
            if completed.returncode == 0:
                return parsed
            message, session_id = parsed.message, parsed.session_id
        else:
            message, session_id = parse_output(self.engine, completed.stdout)
        if completed.returncode != 0:
            from .process_diagnostics import ErrorTail
            tail = ErrorTail()
            tail.append(completed.stderr.encode("utf-8"))
            cause = tail.description()
            message = f"{self.engine} exited {completed.returncode}" + (f" ({cause})" if cause else "")
        return AgentResult(completed.returncode, message[:12000], session_id, len(message) > 12000)


def record_exec_usage(output, telemetry):
    # One exec invocation starts a single turn. An ambiguous terminal receipt cannot establish usage.
    usage = None
    completed = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            if completed:
                return
            completed = True
            usage = exec_usage(event.get("usage"))
    if usage:
        telemetry(usage)


def parse_output(engine: str, output: str) -> tuple[str, str | None]:
    """Extract best-effort answer and session ID without assuming stable vendor events."""
    if engine == "antigravity":
        parsed = parse_antigravity(output)
        return parsed.message, parsed.session_id
    if engine == "claude":
        try:
            payload = json.loads(output)
            if isinstance(payload, dict):
                return (str(payload.get("result") or payload.get("message") or output),
                        payload.get("session_id"))
        except (ValueError, TypeError):
            pass
        return output.strip(), None

    if engine != "codex":
        raise ValueError(f"{engine} requires its own transport decoder")

    last_message = ""
    session_id = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started":
            session_id = event.get("thread_id")
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                last_message = str(item.get("text", ""))
    return last_message or output.strip(), session_id


def parse_antigravity(output: str) -> AgentResult:
    """Require one terminal result; transport exit zero alone is not success."""
    invalid = AgentResult(1, "Antigravity did not return one valid successful result.")
    try:
        whole = json.loads(output)
    except (ValueError, TypeError):
        whole = None
    if isinstance(whole, dict) and "event" not in whole and "result" not in whole and "status" in whole:
        results, tool_error = [whole], False
    else:
        results, tool_error = [], False
        records = [whole] if isinstance(whole, dict) else []
        for line in ([] if records else output.split("\n")):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                return invalid
            if not isinstance(event, dict):
                return invalid
            records.append(event)
        for index, event in enumerate(records):
            if event.get("event") not in ("init", "step_update", "result") or any(key in event for key in ("status", "response", "error")):
                return invalid
            if event.get("event") == "result":
                if index != len(records) - 1:
                    return invalid
                results.append(event.get("result"))
            if event.get("event") == "step_update":
                step = event.get("step_update")
                if not isinstance(step, dict):
                    return invalid
                tool = step.get("tool_info")
                if tool is not None and not isinstance(tool, dict):
                    return invalid
                tool_error = tool_error or bool(tool is not None and "error" in tool and tool["error"] is not None)
    if len(results) != 1 or not isinstance(results[0], dict):
        return invalid
    result = results[0]
    status = result.get("status")
    if status == "INTERRUPTED":
        return AgentResult(124, "Antigravity reported interruption.")
    response = result.get("response")
    if status != "SUCCESS" or ("error" in result and result["error"] is not None) or tool_error or not isinstance(response, str) or not response.strip():
        return invalid
    session = result.get("conversation_id")
    if session is not None and not isinstance(session, str):
        return invalid
    return AgentResult(0, response[:12000], session, len(response) > 12000)

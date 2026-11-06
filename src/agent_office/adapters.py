"""Provider-specific command adapters. Never invoke agent CLIs with shell=True."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from .engines import SUPPORTED_ENGINES


@dataclass(frozen=True)
class AgentResult:
    returncode: int
    message: str
    session_id: str | None = None
    truncated: bool = False


class CLIAdapter:
    def __init__(self, engine: str, timeout: int = 900):
        if engine not in SUPPORTED_ENGINES:
            raise ValueError(f"Unsupported engine: {engine}")
        self.engine = engine
        self.timeout = timeout

    def command(self, prompt: str, *, allow_edits: bool) -> list[str]:
        if self.engine == "antigravity":
            return ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
                    "--mode", "accept-edits" if allow_edits else "plan", "--sandbox",
                    "--disable-slash-commands", "--print-timeout", f"{self.timeout}s"]
        if self.engine == "claude":
            return [
                "claude", "-p", "--output-format", "json",
                "--permission-mode", "acceptEdits" if allow_edits else "plan",
                "--max-turns", "16",
            ]
        return [
            "codex", "exec", "--json", "--sandbox",
            "workspace-write" if allow_edits else "read-only", "-",
        ]

    def execute(self, prompt: str, *, workspace: Path, allow_edits: bool) -> AgentResult:
        # Pass untrusted task text via stdin: safer for Windows .cmd launchers,
        # long prompts, and special characters than putting it in argv.
        argv = self.command(prompt, allow_edits=allow_edits)
        executable = shutil.which(argv[0])
        if executable is None:
            return AgentResult(127, f"Missing CLI: {argv[0]}. Install and authenticate it first.")
        argv[0] = executable
        # Python-based tools launched on Windows otherwise inherit a legacy
        # console encoding even though the parent writes UTF-8 to stdin.
        # For Node-based CLIs this has no behavioral effect.
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        task_input = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n" if self.engine == "antigravity" else prompt
        try:
            completed = subprocess.run(
                argv, cwd=workspace, input=task_input, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(124, f"{self.engine} timed out after {self.timeout} seconds")
        except OSError as exc:
            return AgentResult(126, f"Unable to start {self.engine}: {exc}")

        if self.engine == "antigravity":
            parsed = parse_antigravity(completed.stdout)
            if completed.returncode == 0:
                return parsed
            message, session_id = parsed.message, parsed.session_id
        else:
            message, session_id = parse_output(self.engine, completed.stdout)
        if completed.returncode != 0:
            error = completed.stderr.strip() or message
            message = f"{self.engine} exited {completed.returncode}: {error[:3000]}"
        return AgentResult(completed.returncode, message[:12000], session_id, len(message) > 12000)


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
    if isinstance(whole, dict) and "status" in whole:
        results, tool_error = [whole], False
    else:
        results, tool_error = [], False
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                return invalid
            if not isinstance(event, dict):
                return invalid
            if event.get("event") == "result":
                results.append(event.get("result"))
            if event.get("event") == "step_update":
                step = event.get("step_update")
                if not isinstance(step, dict):
                    return invalid
                tool = step.get("tool_info")
                if tool is not None and not isinstance(tool, dict):
                    return invalid
                tool_error = tool_error or bool(tool and tool.get("error"))
    if len(results) != 1 or not isinstance(results[0], dict):
        return invalid
    result = results[0]
    status = result.get("status")
    if status == "INTERRUPTED":
        return AgentResult(124, "Antigravity reported interruption.")
    response = result.get("response")
    if status != "SUCCESS" or result.get("error") or tool_error or not isinstance(response, str) or not response.strip():
        return invalid
    session = result.get("conversation_id")
    if session is not None and not isinstance(session, str):
        return invalid
    return AgentResult(0, response[:12000], session, len(response) > 12000)

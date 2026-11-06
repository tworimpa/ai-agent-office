"""Provider-specific command adapters. Never invoke agent CLIs with shell=True."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_ENGINES = ("claude", "codex")


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
        try:
            completed = subprocess.run(
                argv, cwd=workspace, input=prompt, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(124, f"{self.engine} timed out after {self.timeout} seconds")
        except OSError as exc:
            return AgentResult(126, f"Unable to start {self.engine}: {exc}")

        message, session_id = parse_output(self.engine, completed.stdout)
        if completed.returncode != 0:
            error = completed.stderr.strip() or message
            message = f"{self.engine} exited {completed.returncode}: {error[:3000]}"
        return AgentResult(completed.returncode, message[:12000], session_id, len(message) > 12000)


def parse_output(engine: str, output: str) -> tuple[str, str | None]:
    """Extract best-effort answer and session ID without assuming stable vendor events."""
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

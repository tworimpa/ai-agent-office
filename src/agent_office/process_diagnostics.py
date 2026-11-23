"""Bounded stderr retention and termination of a dedicated CLI process group."""
import asyncio
import os
from pathlib import Path
import re
import signal
import subprocess
import threading

from .events import redact_request


class ErrorTail:
    def __init__(self):
        self.buffer = bytearray()
        self.wrapped = False

    def append(self, block):
        self.buffer.extend(block)
        if len(self.buffer) > 16384:
            del self.buffer[:-16384]
            self.wrapped = True

    def description(self):
        # stderr can echo a prompt, a diff, or arbitrary secrets. Publish only
        # recognized causes, even when there are no recognizable token prefixes.
        data = bytes(self.buffer)
        if self.wrapped:
            data = data.partition(b"\n")[2]
        lines = redact_request(data.decode("utf8", errors="replace")).splitlines()
        for line in reversed(lines):
            for pattern, label in _CAUSES:
                if re.search(pattern, line, re.I):
                    return label
        return "unclassified stderr" if self.buffer else ""


_CAUSES = (
    (r"model[_ -]?not[_ -]?found|model does not exist", "model_not_found"),
    (r"unauthenticated|unauthorized|invalid[_ -]?grant|(?:oauth|token).*expired|401\b", "authentication failed"),
    (r"rate[_ -]?limit|quota[_ -]?exceeded|429\b", "provider rate limit"),
    (r"unknown (?:option|flag|argument)|unrecognized arguments?", "unsupported CLI option"),
    (r"certificate|\btls\b|\bssl\b", "TLS certificate error"),
    (r"proxy|econnrefused|connection refused", "network connection failed"),
    (r"database (?:table )?is locked|SQLITE_BUSY", "SQLite database locked"),
    (r"readonly database|read-only database|SQLITE_READONLY", "SQLite database read-only"),
    (r"TimeoutExpired|TimeoutError|deadline exceeded", "process timeout"),
    (r"ModuleNotFoundError|ImportError", "Python module import failed"),
    (r"PermissionError|permission denied|access is denied", "permission denied"),
    (r"FileNotFoundError|no such file or directory", "file not found"),
    (r"Traceback \(most recent", "Python traceback"),
)


def drain_pipe(stream, receive):
    def read():
        with stream:
            while block := stream.read1(8192):
                receive(block)
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread


async def drain_async(stream, receive):
    while block := await stream.read(8192):
        receive(block)


def kill_family(pid):
    if os.name == "nt":
        killer = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(killer), "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def close_stdio_process(process):
    process.stdin.close()
    if process.returncode is None:
        await asyncio.to_thread(kill_family, process.pid)
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.wait(), timeout=3)



def diagnostic_result(message):
    if not isinstance(message, str):
        return None
    prefix = re.match(r"^(codex|antigravity|claude|gemini|cline) (exited -?[0-9]+|protocol failure)", message)
    if prefix:
        causes = ["unclassified stderr", *(label for _, label in _CAUSES)]
        for cause in causes:
            if message.endswith("(" + cause + ")"):
                return prefix[0] + " (" + cause + ")"
    return None

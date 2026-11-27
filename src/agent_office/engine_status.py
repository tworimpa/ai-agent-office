"""Fixed metadata probes only. Never inspect credentials or call a model."""
from datetime import datetime, timezone
import os
import re
import shutil  # Also used by executable resolution.
import subprocess
import sys
import tempfile

from .process_window import BACKGROUND_FLAGS
from .engines import CLINE_ACP_VERSION, cline_compatible, ENGINE_COMMANDS, INITIAL_ENGINES, resolve_executable

LABELS = {"codex": "Codex", "antigravity": "Antigravity", "claude": "Claude Code",
          "gemini": "Gemini CLI", "cline": "Cline CLI"}
FLAGS = {
    "codex": {"--json", "--sandbox"},
    "antigravity": {"--input-format", "--output-format", "--mode", "--sandbox",
                    "--disable-slash-commands", "--print-timeout"},
    # Claude can omit its documented --max-turns option from help.
    # The adapter always supplies the turn limit when it runs Claude.
    "claude": {"--print", "--output-format", "--permission-mode"},
    "gemini": {"--acp", "--approval-mode"},
    "cline": {"--acp", "--auto-approve"},
}


def _probe(argv, timeout):
    from .github_connection import worker_env
    try:
        # Help and version should not inherit interactive input or project configuration.
        with tempfile.TemporaryDirectory(prefix="office-metadata-") as cwd:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                result = subprocess.run(argv, cwd=cwd, env=worker_env(os.environ), stdin=subprocess.DEVNULL, stdout=stdout,
                                        stderr=stderr, timeout=timeout, check=False, creationflags=BACKGROUND_FLAGS)
                for name, stream in (("stdout", stdout), ("stderr", stderr)):
                    stream.seek(0)
                    if getattr(result, name) is None:
                        setattr(result, name, stream.read(128 * 1024).decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "error"
    return result, "ok" if result.returncode == 0 else "error"


def _auth_status(result):
    if result is None:
        return "unknown"
    text = result.stdout + "\n" + result.stderr
    authenticated = bool(re.search(r"(?im)^\s*Logged in using\b", text))
    unauthenticated = bool(re.search(r"(?im)^\s*Not logged in\b", text))
    if result.returncode == 0 and authenticated and not unauthenticated:
        return "authenticated"
    if unauthenticated and not authenticated:
        return "not_authenticated"
    return "unknown"


def metadata_entry(engine, executable):
    default_role = {"codex": "developer", "antigravity": "reviewer"}.get(engine)
    return {"engine": engine, "command": ENGINE_COMMANDS[engine], "label": LABELS[engine],
            "default_role": default_role, "installed": executable is not None, "version": None,
            "probe_status": "pending" if executable is not None else "missing",
            "auth_status": "unknown", "headless_support": False, "execution_status": "not_checked"}


_VERSION_LABELS = {
    "codex": r"codex(?:[- ]cli)?",
    "antigravity": r"(?:antigravity(?: cli)?|agy)",
    "claude": r"claude(?: code)?",
    "gemini": r"gemini(?: cli)?",
    "cline": r"cline(?: cli)?",
}
_VERSION = r"v?(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)"


def probe_version(engine, executable, timeout):
    result, status = _probe([executable, "--version"], timeout)
    if status != "ok":
        return None, status
    label = _VERSION_LABELS[engine]
    pattern = re.compile(
        rf"(?:(?:{label}\s+)?version\s*:?\s+|{label}\s+)?{_VERSION}(?:\s+\((?:{label})\))?", re.I
    )
    versions = set()
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        match = pattern.fullmatch(line.strip())
        if match:
            versions.add(match["version"])
    if len(versions) == 1:
        version = versions.pop()
        if len(version) <= 80:
            return version, status
    return None, "unknown_version"


def probe_engine(engine, executable, timeout=5):
    entry = metadata_entry(engine, executable)
    if executable is None:
        return entry
    entry["version"], entry["probe_status"] = probe_version(engine, executable, timeout)
    if entry["probe_status"] == "ok":
        help_args = [executable, "exec", "--help"] if engine == "codex" else [executable, "--help"]
        help_result, help_status = _probe(help_args, timeout)
        if help_status == "ok":
            help_text = help_result.stdout + "\n" + help_result.stderr
            advertised = set(re.findall(r"--[a-z][a-z-]*", help_text))
            entry["profile_flags"] = sorted(advertised & {"--model", "--effort"})
            effort_help = re.search(r"(?m)^\s*(?:-\w,\s*)?--effort\b([^\n]*(?:\n[ \t]+\([^\n)]*\))?)", help_text)
            if effort_help:
                entry["profile_efforts"] = [level for level in ("low","medium","high","xhigh","max")
                                            if re.search(rf"\b{level}\b",effort_help[1])]
            entry["headless_support"] = FLAGS[engine] <= advertised
            if engine == "gemini":
                modes = set(re.findall(r"[A-Za-z0-9_-]+", help_text))
                entry["headless_support"] &= {"plan", "auto_edit"} <= modes
            entry["probe_status"] = "ok" if entry["headless_support"] else "unsupported"
        else:
            entry["probe_status"] = help_status
        if engine == "cline" and entry["probe_status"] == "ok" and entry["version"] != CLINE_ACP_VERSION:
            entry["probe_status"] = "unverified_version"
        if engine == "cline" and not cline_compatible(entry["version"]):
            entry["headless_support"] = False
            entry["probe_status"] = "unsupported_version"
        if engine == "codex":
            auth, _ = _probe([executable, "login", "status"], timeout)
            entry["auth_status"] = _auth_status(auth)
    return entry


def collect_engine_status(timeout: int = 5) -> dict:
    if type(timeout) is not int or not 1 <= timeout <= 15:
        raise ValueError("Metadata timeout must be from 1 to 15 seconds")
    entries = [probe_engine(engine, resolve_executable(ENGINE_COMMANDS[engine]), timeout)
               for engine in INITIAL_ENGINES]
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "platform": sys.platform, "engines": entries}

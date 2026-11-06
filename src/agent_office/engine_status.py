"""Fixed metadata probes only. Never inspect credentials or call a model."""
from datetime import datetime, timezone
import re
import shutil
import subprocess
import sys
from .engines import ENGINE_COMMANDS, INITIAL_ENGINES, resolve_executable

FLAGS = {
    "codex": {"--json", "--sandbox"},
    "antigravity": {"--input-format", "--output-format", "--mode", "--sandbox", "--disable-slash-commands", "--print-timeout"},
}


def _probe(argv, timeout):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
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


def collect_engine_status(timeout: int = 5) -> dict:
    if type(timeout) is not int or not 1 <= timeout <= 15:
        raise ValueError("Metadata timeout must be from1 to15 seconds")
    entries = []
    for engine in INITIAL_ENGINES:
        command = ENGINE_COMMANDS[engine]
        executable = resolve_executable(command)
        entry = {"engine": engine, "command": command, "label": "Codex" if engine == "codex" else "Antigravity",
                 "default_role": "developer" if engine == "codex" else "reviewer",
                 "installed": executable is not None, "version": None, "probe_status": "missing",
                 "auth_status": "unknown", "headless_support": False, "execution_status": "not_checked"}
        if executable is not None:
            version, status = _probe([executable, "--version"], timeout)
            entry["probe_status"] = status
            if status == "ok":
                match = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b", version.stdout + "\n" + version.stderr)
                entry["version"] = match[1][:80] if match else None
                help_args = [executable, "exec", "--help"] if engine == "codex" else [executable, "--help"]
                help_result, help_status = _probe(help_args, timeout)
                if help_status == "ok":
                    advertised = set(re.findall(r"--[a-z][a-z-]*", help_result.stdout + "\n" + help_result.stderr))
                    entry["headless_support"] = FLAGS[engine] <= advertised
                    entry["probe_status"] = "ok" if entry["headless_support"] else "unsupported"
                else:
                    entry["probe_status"] = help_status
                if engine == "codex":
                    auth, _ = _probe([executable, "login", "status"], timeout)
                    entry["auth_status"] = _auth_status(auth)
        entries.append(entry)
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "platform": sys.platform, "engines": entries}

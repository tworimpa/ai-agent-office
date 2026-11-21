"""Per-journal, host-specific evidence for operator-initiated provider fixtures."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from .fixture_process import execute_fixture
from .engine_status import metadata_entry, probe_engine, probe_version
from .engines import CLINE_ACP_VERSION, ENGINE_COMMANDS, SUPPORTED_ENGINES, resolve_executable
from .store import TaskStore, now
from .workflow import parse_review_verdict


CAPABILITIES = ("read", "edit", "review")
# Increment the relevant revision when the adapter contract or fixture changes.
_PROTOCOLS = {"codex": "exec-jsonl", "antigravity": "stream-json", "claude": "print-json",
              "gemini": "acp", "cline": "acp"}
_FIXTURE_REVISION = 2
_ADAPTER_REVISIONS = {engine: 1 for engine in _PROTOCOLS}
_METADATA_TTL = 300
_TABLE = """CREATE TABLE IF NOT EXISTS studio_engine_checks (
    runtime TEXT NOT NULL, engine TEXT NOT NULL, capability TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('passed', 'failed')), checked_at TEXT NOT NULL,
    PRIMARY KEY(runtime, engine, capability))"""


def _runtime(engine):
    executable = resolve_executable(ENGINE_COMMANDS[engine])
    if executable is None:
        return None, None
    try:
        path = Path(executable).resolve(strict=True)
        info = path.stat()
    except OSError:
        return None, None
    return executable, (os.path.normcase(str(path)), info.st_dev, info.st_ino,
                        info.st_size, info.st_mtime_ns)


def _platform():
    if sys.platform.startswith("linux") and "microsoft" in platform.release().lower():
        return "wsl"
    return sys.platform


def _fingerprint(engine, stamp, version):
    if stamp is None or version is None or (engine == "cline" and version != CLINE_ACP_VERSION):
        return None
    identity = (_platform(), socket.gethostname(), stamp, version, engine, _PROTOCOLS[engine],
                _ADAPTER_REVISIONS[engine], _FIXTURE_REVISION)
    return sha256(json.dumps(identity).encode("utf-8")).hexdigest()


class StudioEngineCatalog:
    def __init__(self, db_path):
        self.path = Path(db_path)
        with TaskStore(self.path) as store:
            store.connection.execute(_TABLE)
        self._lock = threading.Lock()
        self._cache = {}
        self._closed = False
        self._checked_at = now()

    def close(self):
        with self._lock:
            self._closed = True

    def _collect(self, engine, executable, record):
        entry = probe_engine(engine, executable)
        with self._lock:
            if not self._closed and self._cache.get(engine) is record:
                record["entry"] = entry
                ttl = 10 if entry["probe_status"] in {"timeout", "error"} else _METADATA_TTL
                record["expires"] = time.monotonic() + ttl
                self._checked_at = now()

    def snapshot(self):
        entries = []
        timestamp = time.monotonic()
        with self._lock:
            for engine in SUPPORTED_ENGINES:
                executable, stamp = _runtime(engine)
                record = self._cache.get(engine)
                if record is None or record["stamp"] != stamp or timestamp >= record["expires"]:
                    record = {"stamp": stamp, "entry": metadata_entry(engine, executable),
                              "expires": float("inf")}
                    self._cache[engine] = record
                    self._checked_at = now()
                    if executable and not self._closed:
                        threading.Thread(target=self._collect, args=(engine, executable, record),
                                         daemon=True).start()
                entry = record["entry"].copy()
                entry["protocol"] = _PROTOCOLS[engine]
                entries.append((entry, _fingerprint(engine, stamp, entry["version"])))
            checked_at = self._checked_at

        with TaskStore(self.path, readonly=True) as store:
            for entry, fingerprint in entries:
                capabilities = {name: {"status": "not_checked", "checked_at": None} for name in CAPABILITIES}
                if fingerprint:
                    rows = store.connection.execute(
                        "SELECT capability,status,checked_at FROM studio_engine_checks WHERE runtime=? AND engine=?",
                        (fingerprint, entry["engine"]),
                    )
                    for capability, status, when in rows:
                        if capability in capabilities:
                            capabilities[capability] = {"status": status, "checked_at": when}
                entry["capabilities"] = capabilities
        return {"platform": _platform(), "checked_at": checked_at, "engines": [entry for entry, _ in entries]}


def _fixture_tree(root):
    """Bound reads and reject links, junctions, special files, and extra-large fixtures."""
    contents = {}
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Fixture contains a redirected path")
        name = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            contents[name] = (info.st_mode, None)
            with os.scandir(path) as children:
                for child in children:
                    if len(pending) + len(contents) >= 64:
                        raise ValueError("Fixture exceeds its entry limit")
                    pending.append(Path(child.path))
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 16 * 1024:
            with path.open("rb") as stream:
                data = stream.read(16 * 1024 + 1)
            if len(data) > 16 * 1024:
                raise ValueError("Fixture exceeds its file size limit")
            contents[name] = (info.st_mode, data)
        else:
            raise ValueError("Fixture contains an unsupported file")
    return contents


def _run_fixture(engine, capability):
    git = resolve_executable("git")
    if not git:
        return False, "fixture_setup_failed"
    marker = uuid4().hex
    wrong = uuid4().hex
    with tempfile.TemporaryDirectory(prefix="office-engine-check-") as temp:
        workspace = Path(temp) / "repo"
        workspace.mkdir()
        git_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        subprocess.run([git, "init", "--quiet", "--template="], cwd=workspace, env=git_env, timeout=10,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        (workspace / "reference.txt").write_text(marker + "\n", encoding="ascii")
        candidate = workspace / "candidate.txt"
        if capability == "read":
            prompt = "Read reference.txt. Reply with only its exact single-line value. Leave every file unchanged."
        elif capability == "edit":
            candidate.write_text("unfilled\n", encoding="ascii")
            prompt = ("Edit only candidate.txt so its single-line value exactly matches reference.txt. "
                      "Use the value you read from the file.")
        else:
            candidate.write_text(wrong + "\n", encoding="ascii")
            prompt = ("Review whether candidate.txt exactly matches reference.txt. Leave every file unchanged. "
                      "Give a standalone VERDICT: APPROVED or VERDICT: CHANGES_REQUESTED line. "
                      "Report the exact reference and candidate values on separate lines as Expected: VALUE and Found: VALUE.")
        before = _fixture_tree(Path(temp))
        result = execute_fixture(
            engine, prompt + " Stay in this disposable repository. Do not commit, push, or change permissions.",
            workspace=workspace, allow_edits=capability == "edit",
        )
        if result.returncode != 0 or result.truncated:
            return False, "provider_failed"
        after = _fixture_tree(Path(temp))
        if capability == "edit":
            # Test the edited value independently without executing candidate code.
            _, data = after.get("repo/candidate.txt", (None, None))
            if data is None or data.splitlines() != [marker.encode("ascii")]:
                return False, "fixture_mismatch"
            before["repo/candidate.txt"] = (before["repo/candidate.txt"][0], data)
        if before != after:
            return False, "unexpected_change"
        if capability == "read" and result.message.strip() != marker:
            return False, "fixture_mismatch"
        if capability == "review":
            lines = result.message.splitlines()
            if (parse_review_verdict(result.message) != "CHANGES_REQUESTED"
                    or f"Expected: {marker}" not in lines or f"Found: {wrong}" not in lines):
                return False, "fixture_mismatch"
        return True, "fixture_passed"


def verify_engine(db_path, engine, capability):
    """Only the explicit local CLI subcommand calls a model. Persist fixed enums and UTC times."""
    if engine not in SUPPORTED_ENGINES or capability not in CAPABILITIES:
        raise ValueError("지원 엔진과 검증 항목(read/edit/review)을 확인하세요.")
    with TaskStore(db_path) as store:
        store.connection.execute(_TABLE)
        executable, stamp = _runtime(engine)
        version = probe_version(engine, executable, 5)[0] if executable else None
        fingerprint = _fingerprint(engine, stamp, version)
        passed = False
        if engine == "cline" and version is not None and version != CLINE_ACP_VERSION:
            reason = "unsupported_version"
        elif not fingerprint:
            reason = "missing" if not executable else "unknown_version"
        else:
            try:
                passed, reason = _run_fixture(engine, capability)
            except (OSError, ValueError, subprocess.SubprocessError):
                reason = "fixture_failed"
            current_executable, current_stamp = _runtime(engine)
            current_version = probe_version(engine, current_executable, 5)[0] if current_executable else None
            if _fingerprint(engine, current_stamp, current_version) != fingerprint:
                passed, reason, fingerprint = False, "runtime_changed", None
        result = {"engine": engine, "capability": capability, "status": "passed" if passed else "failed",
                  "checked_at": now(), "platform": _platform(), "version": version, "reason": reason}
        if fingerprint:
            store.connection.execute(
                "INSERT INTO studio_engine_checks VALUES(?,?,?,?,?) "
                "ON CONFLICT(runtime,engine,capability) DO UPDATE SET "
                "status=excluded.status, checked_at=excluded.checked_at WHERE excluded.checked_at>=checked_at",
                (fingerprint, engine, capability, result["status"], result["checked_at"]),
            )
        return result

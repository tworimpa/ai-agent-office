"""Fixed loopback probes and native controls for an already provisioned service."""
from contextlib import contextmanager
import ctypes
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time

from .models import MAX_SQLITE_INT, validate_id
from .store import TaskStore


class JournalBusy(RuntimeError):
    """The bound Studio journal cannot be reserved for an idle-only stop."""


class _IdleJournal:
    def __init__(self, path, expected):
        self.path = Path(path).expanduser().resolve(strict=True)
        self.identity = self.path.stat()
        self.expected = validate_id(expected)
        self.store = None

    def verify(self, registered_path):
        if (self.path != Path(registered_path)
                or not os.path.samestat(self.identity, self.path.stat())):
            raise ValueError("Service is not using the reserved runtime journal")

    def acquire(self):
        self.verify(self.path)
        if self.store is not None:
            return
        try:
            self.store = TaskStore(self.path, existing=True)
            db = self.store.connection
            db.execute("PRAGMA busy_timeout=0")
            db.execute("BEGIN IMMEDIATE")
            self.verify(self.path)
            row = db.execute("SELECT id FROM studio_identity WHERE singleton=1").fetchone()
            if row is None or row[0] != self.expected:
                raise ValueError("Unexpected runtime journal identity")
            if db.execute("SELECT 1 FROM runs WHERE ended_at IS NULL LIMIT 1").fetchone():
                raise JournalBusy("Runtime journal has an active run")
            # Improvement generation launches a separate provider supervisor.
            # Older Studio journals predate this table.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_improvements'").fetchone():
                from .studio_improvements import _slot
                if (db.execute("SELECT 1 FROM studio_improvements WHERE state='generating' LIMIT 1").fetchone()
                        or _slot(self.path).is_held()):
                    raise JournalBusy("Runtime journal has an active improvement job")
        except sqlite3.OperationalError as exc:
            self()
            if getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise JournalBusy("Runtime journal has an active writer") from exc
            raise
        except BaseException:
            self()
            raise

    def __call__(self):
        if self.store is not None:
            self.store.close()  # Releases the unmodified write transaction.
            self.store = None


@contextmanager
def idle_journal(path, expected_journal):
    """Reserve existing Studio admissions through the predecessor's native stop."""
    lease = _IdleJournal(path, expected_journal)
    try:
        lease.acquire()
        yield lease
    finally:
        lease()


def validate_config(config):
    for key in ("target_id", "release_id", "journal_id"):
        validate_id(config[key])
    manager, service, port = config["manager"], config["service"], config["port"]
    if manager not in ("systemd", "systemd-user", "windows"):
        raise ValueError("Unsupported service manager")
    if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}", service):
        raise ValueError("Invalid service name")
    if manager != "windows" and not service.endswith(".service"):
        raise ValueError("A systemd service unit name ending in .service is required")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("A fixed loopback port is required")


class ForeignRuntime(ValueError):
    pass


def _receipt(config, identity):
    keys = {"target_id", "release_id", "journal_id", "activation_id", "boot_id", "seq", "sha", "port", "pid"}
    if not isinstance(identity, dict) or identity.keys() != keys:
        raise ValueError("Expected managed Studio identity")
    for key in ("target_id", "release_id", "journal_id", "activation_id", "boot_id"):
        validate_id(identity[key])
    if (type(identity["port"]) is not int or not 1 <= identity["port"] <= 65535
            or type(identity["pid"]) is not int or not 0 < identity["pid"] < 2**32
            or type(identity["seq"]) is not int or not 1 <= identity["seq"] <= MAX_SQLITE_INT
            or not isinstance(identity["sha"], str)
            or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", identity["sha"])):
        raise ValueError("Invalid running version")
    if any(identity[key] != config[key] for key in ("target_id", "release_id", "journal_id", "port")):
        raise ForeignRuntime("A different managed Studio answered")
    return identity


def _read(port, route, deadline):
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise TimeoutError()
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout)
    timer = None
    try:
        conn.connect()
        sock = conn.sock

        def expire():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # A loopback listener trickling headers or body bytes must not hold
        # the operator's deployment lock beyond the probe deadline.
        timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        conn.request("GET", route)
        response = conn.getresponse()
        data = None
        if response.status == 200 and response.headers.get_content_type() == "application/json":
            raw = response.read(65537)
            if len(raw) <= 65536:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (ValueError, RecursionError):
                    pass
        return response.status, data, response.getheader("X-Agent-Office-Boot")
    finally:
        if timer:
            timer.cancel()
        conn.close()
        if time.monotonic() >= deadline:
            raise TimeoutError()


def probe(config, timeout=4.0):
    """Verify the boot receipt and a normal Studio request from the same boot."""
    port = config["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("A fixed loopback port is required")
    for key in ("target_id", "release_id", "journal_id"):
        validate_id(config[key])
    deadline = time.monotonic() + timeout
    identity = None
    try:
        status, data, _ = _read(port, "/api/runtime-health", deadline)
        if status != 200:
            return {"state": "unhealthy", "detail": "Runtime identity endpoint failed", "identity": None}
        identity = _receipt(config, data)
        status, data, boot = _read(port, "/api/hosts", deadline)
        if boot is not None and boot != identity["boot_id"]:
            return {"state": "wrong_identity", "detail": "Host directory answered from a different boot", "identity": None}
        if boot is None:
            return {"state": "unhealthy", "detail": "Missing host directory boot ID", "identity": None}
        if (status != 200 or not isinstance(data, dict) or not isinstance(data.get("hosts"), list)
                or not any(isinstance(host, dict) and host.get("id") == "local" for host in data["hosts"])):
            return {"state": "unhealthy", "detail": "Studio host directory unavailable", "identity": identity}
        return {"state": "healthy", "detail": "", "identity": identity}
    except ConnectionRefusedError:
        state, detail = ("unhealthy" if identity else "refused"), "Loopback connection refused"
    except TimeoutError:
        state, detail = "timeout", "Loopback check timed out"
    except ForeignRuntime:
        state, detail = "wrong_identity", "A different managed Studio answered"
    except (ValueError, RecursionError):
        state, detail = "unhealthy", "Managed Studio identity unavailable"
    except (OSError, HTTPException):
        state, detail = "unhealthy", "Loopback HTTP check failed"
    return {"state": state, "detail": detail, "identity": None}


def _windows_sc():
    # Windows, not PATH or the invoking shell, owns this executable.
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise RuntimeError("Windows system directory is unavailable")
    return str(Path(buffer.value) / "sc.exe")


def _command(config, action):
    validate_config(config)
    manager, service = config["manager"], config["service"]
    if manager == "windows" and sys.platform == "win32":
        if action in ("start", "stop"):
            return [_windows_sc(), action, service]
    elif manager != "windows" and sys.platform.startswith("linux"):
        command = ["/usr/bin/systemctl", "--user" if manager == "systemd-user" else "--system",
                   "--no-ask-password", "--no-pager"]
        if action == "query":
            return [*command, "show", "--property=ActiveState", "--property=LoadState", "--", service]
        if action in ("restart", "stop", "start"):
            return [*command, action, "--", service]
    raise ValueError("Unsupported operation for the native service manager")


def _run(config, action):
    command = _command(config, action)
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Service manager command failed or timed out") from exc


def _windows_state(service):
    import pywintypes
    import win32service as scm
    try:
        manager = scm.OpenSCManager(None, None, scm.SC_MANAGER_CONNECT)
        try:
            handle = scm.OpenService(manager, service, scm.SERVICE_QUERY_STATUS)
            try:
                state = scm.QueryServiceStatus(handle)[1]
            finally:
                scm.CloseServiceHandle(handle)
        finally:
            scm.CloseServiceHandle(manager)
    except pywintypes.error:
        return "unknown"
    return {scm.SERVICE_STOPPED: "stopped", scm.SERVICE_START_PENDING: "starting",
            scm.SERVICE_STOP_PENDING: "stopping", scm.SERVICE_RUNNING: "running",
            scm.SERVICE_PAUSED: "paused", scm.SERVICE_PAUSE_PENDING: "pausing",
            scm.SERVICE_CONTINUE_PENDING: "continuing"}.get(state, "unknown")


def manager_state(config):
    validate_config(config)
    if config["manager"] == "windows":
        if sys.platform != "win32":
            raise ValueError("Windows service manager requires Windows")
        return _windows_state(config["service"])
    result = _run(config, "query")
    if result.returncode != 0:
        return "unknown"
    fields = dict(line.split(b"=", 1) for line in result.stdout.splitlines() if b"=" in line)
    if fields.get(b"LoadState") != b"loaded":
        return "unknown"
    return {b"active": "running", b"inactive": "stopped", b"activating": "starting",
            b"deactivating": "stopping", b"failed": "stopped"}.get(fields.get(b"ActiveState"), "unknown")


def _wait_transition(config):
    deadline = time.monotonic() + 30
    while True:
        state = manager_state(config)
        if state not in ("starting", "stopping", "pausing", "continuing"):
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError("Service manager transition timed out")
        time.sleep(0.15)


def _require_success(config, action):
    if _run(config, action).returncode != 0:
        raise RuntimeError(f"Service manager {action} failed")


def stop_service(config):
    if config["manager"] == "windows":
        state = _wait_transition(config)
        if state == "stopped":
            return
        if state not in ("running", "paused"):
            raise RuntimeError("Cannot verify service state before stopping")
    _require_success(config, "stop")
    if _wait_transition(config) != "stopped":
        raise RuntimeError("Service did not stop")


def start_service(config):
    _require_success(config, "start")
    if _wait_transition(config) != "running":
        raise RuntimeError("Service did not reach running state")


def restart_service(config):
    if config["manager"] == "windows":
        stop_service(config)
        return start_service(config)
    _require_success(config, "restart")
    if _wait_transition(config) != "running":
        raise RuntimeError("Service did not reach running state")

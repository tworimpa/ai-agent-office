"""Fixed loopback probes and native controls for an already provisioned service."""
import ctypes
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

from .models import MAX_SQLITE_INT, validate_id


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
        if action in ("restart", "stop"):
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
            scm.SERVICE_STOP_PENDING: "stopping", scm.SERVICE_RUNNING: "running"}.get(state, "unknown")


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
        if state not in ("starting", "stopping"):
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
        if state != "running":
            raise RuntimeError("Cannot verify service state before stopping")
    _require_success(config, "stop")
    if _wait_transition(config) != "stopped":
        raise RuntimeError("Service did not stop")


def restart_service(config):
    if config["manager"] == "windows":
        stop_service(config)
        _require_success(config, "start")
    else:
        _require_success(config, "restart")
    if _wait_transition(config) != "running":
        raise RuntimeError("Service did not reach running state")

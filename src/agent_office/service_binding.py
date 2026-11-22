"""Prove which provisioned service would run a target before native control."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from . import runtime_ops
from .release_view import _journal_id


def _bus(manager, *args):
    result = subprocess.run(
        ["/usr/bin/busctl", "--user" if manager == "systemd-user" else "--system",
         "--timeout=3s", "--json=short", *args],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
    )
    if result.returncode or len(result.stdout) > 64 * 1024:
        raise ValueError("Cannot verify the provisioned systemd service")
    return json.loads(result.stdout)["data"]


def _linux(config):
    dest = "org.freedesktop.systemd1"
    manager = config["manager"]
    unit, = _bus(manager, "call", dest, "/org/freedesktop/systemd1", dest + ".Manager",
                 "LoadUnit", "s", config["service"])
    def prop(name):
        return _bus(manager, "get-property", dest, unit, dest + ".Service", name)
    starts = prop("ExecStartEx")
    if not isinstance(starts, list) or len(starts) != 1:
        raise ValueError("Require one direct Studio service launcher")
    executable, command, flags = starts[0][:3]
    if (not command or executable != command[0] or flags or prop("Type") not in ("simple", "exec")):
        raise ValueError("Require a simple or exec service with a direct, unmodified launcher")
    import pwd
    account = prop("User")
    if account:
        uid = int(account) if account.isdecimal() else pwd.getpwnam(account).pw_uid
    elif manager == "systemd-user":
        # Query the actual bus owner; the client need not own the manager it talks to.
        uid, = _bus(manager, "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "GetConnectionUnixUser", "s", dest)
    else:
        uid = 0
    if type(uid) is not int or uid <= 0:
        raise ValueError("Configure a dedicated non-root Studio service account")
    return {"pid": prop("MainPID"), "binding": [executable, uid, prop("Group")], "uid": uid, "command": command}


def _validate_launcher(command, config, target):
    # Preserve the interpreter path through a venv symlink: two environments
    # can share the same executable but have different packages and startup code.
    if (not isinstance(command, list) or len(command) < 8 or not all(isinstance(x, str) for x in command)
            or not os.path.isabs(command[0])
            or os.path.normcase(os.path.normpath(command[0])) != os.path.normcase(config["runtime_python"])
            or command[1:4] != ["-I", "-m", "agent_office.managed_runtime"]):
        raise ValueError("Service command does not use the registered isolated runtime launcher")
    options = {}
    args = iter(command[4:])
    try:
        for flag in args:
            if flag == "--remote":
                from .studio_ssh import RemoteHost
                RemoteHost(next(args), int(next(args)), next(args))
                continue
            if flag not in ("--target", "--db", "--improvement-source", "--release-view", "--accept-remote") or flag in options:
                raise ValueError("Unsupported service launcher option")
            if flag == "--accept-remote":
                options[flag] = True
            else:
                path = Path(next(args))
                if not path.is_absolute():
                    raise ValueError("Use absolute service paths")
                options[flag] = path.resolve()
    except StopIteration:
        raise ValueError("Incomplete service command") from None
    if (options.get("--target") != Path(target).resolve() or "--db" not in options or
            options["--db"].is_relative_to(options["--target"]) or _journal_id(options["--db"]) != config["journal_id"]):
        raise ValueError("Service command belongs to a different deployment target or runtime journal")
    return options["--db"]


def inspect_service(config, target, *, for_control=True):
    """Return restart-stable enrollment evidence and the volatile native owner PID."""
    runtime_ops.validate_config(config)
    try:
        if config["manager"] == "windows" and os.name == "nt":
            from .windows_binding import inspect_windows, operator_sid
            native = inspect_windows(config, target)
            same_account = for_control and native["account_sid"] == operator_sid()
        elif config["manager"] in ("systemd", "systemd-user") and sys.platform.startswith("linux"):
            native = _linux(config)
            same_account = for_control and native["uid"] == os.geteuid()
        else:
            raise ValueError("This target requires its configured native service manager")
        if same_account:
            raise ValueError("Service and deployment operator require separate accounts")
        journal = _validate_launcher(native["command"], config, target)
        pid = native["pid"]
        if type(pid) is not int or pid < 0:
            raise ValueError("Invalid service process owner")
        pinned = [config["manager"], config["service"], native["binding"], native["command"]]
        signature = hashlib.sha256(json.dumps(pinned, ensure_ascii=True).encode("ascii")).hexdigest()
        return {"manager": config["manager"], "service": config["service"], "pid": pid, "signature": signature,
                "journal_path": str(journal)}
    except (KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Cannot verify the registered service binding") from exc


def _linux_owns(pid, port):
    # The launcher binds an IPv4 loopback socket in its own process.
    address = f"0100007F:{port:04X}"
    with open("/proc/net/tcp", encoding="ascii") as tcp:
        rows = [line.split() for line in tcp if address in line]
    owners = {row[9] for row in rows if row[1] == address and row[3] == "0A"}
    if len(owners) != 1:
        return False
    socket_link = "socket:[" + owners.pop() + "]"
    for fd in (Path("/proc") / str(pid) / "fd").iterdir():
        try:
            if os.readlink(fd) == socket_link:
                return True
        except FileNotFoundError:
            continue
    return False


def owns_listener(binding, receipt, port):
    parent = binding["pid"]
    child = receipt["pid"] if receipt else None
    if parent <= 0:
        return False
    try:
        if binding["manager"] == "windows" and os.name == "nt":
            from .windows_binding import owns_windows_process
            return owns_windows_process(child, parent, port)
        return sys.platform.startswith("linux") and (child is None or child == parent) and _linux_owns(parent, port)
    except (OSError, PermissionError):
        return False

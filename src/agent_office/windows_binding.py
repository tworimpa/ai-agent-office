"""Read the configured NSSM service and bind a loopback listener to its SCM process."""
import ctypes
from ctypes import wintypes as win
import json
from pathlib import Path
import re
import socket
import subprocess
import time


def _split(command):
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise ValueError("Missing Windows service command")
    parse = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
    parse.argtypes = [win.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(win.LPWSTR)
    count = ctypes.c_int()
    argv = parse(command, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return list(argv[:count.value])
    finally:
        free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        free.argtypes = [win.HLOCAL]
        free.restype = win.HLOCAL
        free(argv)


def _sc_config(service):
    import win32service as scm

    manager = scm.OpenSCManager(None, None, scm.SC_MANAGER_CONNECT)
    try:
        access = scm.SERVICE_QUERY_CONFIG | scm.SERVICE_QUERY_STATUS
        handle = scm.OpenService(manager, service, access)
        try:
            cfg = scm.QueryServiceConfig(handle)
            if cfg[0] != scm.SERVICE_WIN32_OWN_PROCESS:
                raise ValueError("Expected a dedicated Windows service process")
            return cfg[3], cfg[7], scm.QueryServiceStatusEx(handle)["ProcessId"]
        finally:
            scm.CloseServiceHandle(handle)
    finally:
        scm.CloseServiceHandle(manager)


def _nssm_parameters(service):
    import winreg

    key = rf"SYSTEM\CurrentControlSet\Services\{service}\Parameters"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, access=winreg.KEY_QUERY_VALUE) as params:
        fields = []
        for name in ("Application", "AppParameters"):
            value, kind = winreg.QueryValueEx(params, name)
            if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str) or "%" in value:
                raise ValueError("NSSM requires literal application paths and arguments")
            fields.append(value)
    return fields


def operator_sid():
    import win32api
    import win32con
    import win32security as security
    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid, _ = security.GetTokenInformation(token, security.TokenUser)
        return security.ConvertSidToStringSid(sid)
    finally:
        token.Close()


def _account_sid(account, service):
    import win32api
    import win32security as security
    name = win32api.GetComputerName() + account[1:] if account.startswith(".\\") else account
    sid, _, kind = security.LookupAccountName(None, name)
    value = security.ConvertSidToStringSid(sid)
    virtual = name.casefold() == ("NT SERVICE\\" + service).casefold() and value.startswith("S-1-5-80-")
    if not virtual and (kind != security.SidTypeUser or value.rsplit("-", 1)[-1] in {"500", "501", "502"}):
        raise ValueError("Configure a dedicated non-administrator Studio service account")
    return value


def inspect_windows(config, target):
    """Only SCM metadata and two fixed wrapper fields are read. No service is opened for control."""
    import pywintypes
    from .runtime_ops import validate_config

    validate_config(config)
    if config["manager"] != "windows":
        raise ValueError("Expected a Windows service")
    try:
        image, account, pid = _sc_config(config["service"])
        account_key = (account or "").casefold().replace(" ", "")
        if (not account_key or account_key in {"localsystem", "localservice", "networkservice",
                                               "s-1-5-18", "s-1-5-19", "s-1-5-20"}
                or account_key.startswith("ntauthority\\")):
            raise ValueError("Configure a dedicated non-administrator Studio service account")
        account_sid = _account_sid(account, config["service"])
        image_args = _split(image)
        wrapper = Path(image_args[0])
        if (len(image_args) != 1 or not wrapper.is_absolute() or not re.fullmatch(r"[A-Za-z]:", wrapper.drive)
                or wrapper.name.lower() != "nssm.exe"
                or not wrapper.is_file()):
            raise ValueError("Expected an absolute local NSSM service binary")
        application, arguments = _nssm_parameters(config["service"])
        # argv[0] has special parsing rules on Windows. Supply a dummy image to
        # parse the NSSM argument string exactly as command arguments.
        command = [application, *_split("python.exe " + arguments)[1:]]
    except (OSError, pywintypes.error) as exc:
        raise ValueError("Cannot verify the provisioned Windows service") from exc
    binding = json.dumps([str(wrapper.resolve(strict=True)), account, account_sid], ensure_ascii=False)
    return {"pid": pid, "command": command, "binding": binding, "account_sid": account_sid}


class _TcpRow(ctypes.Structure):
    _fields_ = [(name, win.DWORD) for name in
                ("state", "address", "port", "remote_address", "remote_port", "pid")]


def _listener(port):
    # MIB_TCPTABLE_OWNER_PID from the IPv4 listening table.
    query = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    query.argtypes = [win.LPVOID, ctypes.POINTER(win.DWORD), win.BOOL, win.ULONG, ctypes.c_int, win.ULONG]
    query.restype = win.DWORD
    size = win.DWORD()
    buffer = None
    for _ in range(4):
        code = query(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if code == 0 and buffer is not None:
            break
        if code != 122 or size.value > 32 * 1024 * 1024:  # ERROR_INSUFFICIENT_BUFFER
            raise OSError("Cannot query TCP listening processes")
        buffer = ctypes.create_string_buffer(size.value)
    else:
        raise OSError("TCP table changed during service verification")
    count = win.DWORD.from_buffer(buffer).value
    offset = ctypes.sizeof(win.DWORD)
    if offset + count * ctypes.sizeof(_TcpRow) > len(buffer):
        raise ValueError("Invalid TCP listener table")
    entries = (_TcpRow * count).from_buffer(buffer, offset)
    loopback = int.from_bytes(socket.inet_aton("127.0.0.1"), "little")
    matching = [row for row in entries if row.address in (0, loopback) and socket.ntohs(row.port & 65535) == port]
    return matching[0].pid if len(matching) == 1 and matching[0].address == loopback else None


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("size", win.DWORD), ("usage", win.DWORD), ("pid", win.DWORD),
                ("heap", ctypes.c_size_t), ("module", win.DWORD), ("threads", win.DWORD),
                ("parent", win.DWORD), ("priority", win.LONG), ("flags", win.DWORD),
                ("exe", win.WCHAR * 260)]


def _parents():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel.CreateToolhelp32Snapshot
    snapshot.argtypes = [win.DWORD, win.DWORD]
    snapshot.restype = win.HANDLE
    handle = snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        first, following = kernel.Process32FirstW, kernel.Process32NextW
        for function in (first, following):
            function.argtypes = [win.HANDLE, ctypes.POINTER(_ProcessEntry)]
            function.restype = win.BOOL
        found = {}
        ok = first(handle, ctypes.byref(entry))
        while ok:
            found[entry.pid] = entry.parent
            ok = following(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
        return found
    finally:
        kernel.CloseHandle.argtypes = [win.HANDLE]
        kernel.CloseHandle(handle)


def _birth_time(pid):
    import win32api

    handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    try:
        query = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
        query.argtypes = [win.HANDLE] + [ctypes.POINTER(win.FILETIME)] * 4
        query.restype = win.BOOL
        times = [win.FILETIME() for _ in range(4)]
        if not query(int(handle), *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        handle.Close()


_MAX_ANCESTORS = 64


def _ancestry(listener, parent_pid, parents):
    chain = []
    current = listener
    while current and current not in chain and len(chain) < _MAX_ANCESTORS:
        if current not in parents or type(current) is not int or not 0 < current <= 0xffffffff:
            break
        chain.append(current)
        if current == parent_pid:
            return chain
        current = parents[current]
    raise ValueError("Incomplete Windows service ancestry")


def _cim_birth_times(chain):
    """One local query of the identified PIDs, with no command lines or paths."""
    import win32api

    directory = Path(win32api.GetSystemDirectory())
    shell = directory / "WindowsPowerShell/v1.0/powershell.exe"
    where = " OR ".join(f"ProcessId={pid:d}" for pid in chain)
    script = (
        "$ErrorActionPreference='Stop'; $env:PSModulePath=$PSHOME + '\\Modules'; "
        f"$rows=@(CimCmdlets\\Get-CimInstance -OperationTimeoutSec 3 -Query "
        f"'SELECT ProcessId,ParentProcessId,CreationDate FROM Win32_Process WHERE {where}'); "
        f"if ($rows.Count -ne {len(chain)}) {{ exit 1 }}; "
        "foreach ($r in $rows) { "
        "if ($null -eq $r.ParentProcessId) { exit 1 }; "
        "[Console]::WriteLine(('{0} {1} {2}' -f $r.ProcessId,$r.ParentProcessId,"
        "$r.CreationDate.ToFileTimeUtc())) }"
    )
    result = subprocess.run(
        [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=6, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode or len(result.stdout) > 64 * len(chain):
        raise ValueError("Cannot read Windows process creation metadata")
    records = {}
    for line in result.stdout.splitlines():
        if not re.fullmatch(rb"[0-9]{1,10} [0-9]{1,10} [0-9]{1,19}", line):
            raise ValueError("Invalid Windows process creation metadata")
        pid, parent, birth = map(int, line.split())
        if pid not in chain or pid in records or birth <= 0:
            raise ValueError("Unexpected Windows process record")
        records[pid] = (parent, birth)
    if (len(records) != len(chain) or
            any(records[child][0] != parent for child, parent in zip(chain, chain[1:]))):
        raise ValueError("Windows process ancestry changed")
    return [records[pid][1] for pid in chain]


def owns_windows_process(pid, parent_pid, port, *, service=None):
    """Require the actual listener to be alive under the SCM-owned process."""
    import pywintypes

    if not parent_pid:
        return False
    try:
        # UTC FILETIME uses 100 ns ticks since 1601. A PID created during this
        # observation cannot attest an earlier SCM or listening-table result.
        started = time.time_ns() // 100 + 116444736000000000
        if service is not None and _sc_config(service)[2] != parent_pid:
            return False
        listener = _listener(port)
        if not listener or (pid is not None and listener != pid):
            return False
        chain = _ancestry(listener, parent_pid, _parents())
        if _listener(port) != listener:
            return False
        try:
            births = [_birth_time(process) for process in chain]
            precision = 1
        except (OSError, pywintypes.error) as exc:
            if getattr(exc, "winerror", None) != 5:  # ERROR_ACCESS_DENIED only
                raise
            births = _cim_birth_times(chain)
            precision = 10  # CIM_DATETIME reports microseconds.
        return (0 < births[0] and births[0] + precision <= started and
                all(0 < parent < child for child, parent in zip(births, births[1:])) and
                (service is None or _sc_config(service)[2] == parent_pid) and
                _listener(port) == listener)
    except (ValueError, OSError, pywintypes.error, subprocess.TimeoutExpired):
        return False

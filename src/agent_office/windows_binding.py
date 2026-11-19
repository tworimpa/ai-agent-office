"""Read the configured NSSM service and bind a loopback listener to its SCM process."""
import ctypes
from ctypes import wintypes as win
import json
from pathlib import Path
import re
import socket


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
    import win32process

    handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    try:
        return win32process.GetProcessTimes(handle)["CreationTime"]
    finally:
        handle.Close()


def owns_windows_process(pid, parent_pid, port):
    """Require the actual listener to be alive under the SCM-owned process."""
    import pywintypes

    if not parent_pid:
        return False
    try:
        listener = _listener(port)
        if not listener or (pid is not None and listener != pid):
            return False
        parents = _parents()
        current, born = listener, _birth_time(listener)
        seen = set()
        while current != parent_pid:
            if not current or current in seen:
                return False
            seen.add(current)
            current = parents.get(current)
            if not current:
                return False
            earlier = _birth_time(current)
            if earlier > born:
                return False  # PID recycled after the child was created.
            born = earlier
        return _listener(port) == listener
    except (ValueError, OSError, pywintypes.error):
        return False

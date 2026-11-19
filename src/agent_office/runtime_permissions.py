"""Service-side permission checks for operator-owned deployment inputs."""
from contextlib import contextmanager
import os
import stat


def _protected_paths(target, slot):
    yield target
    yield target / "deployment.sqlite3"
    yield target / "slots"
    yield slot
    yield from slot.rglob("*")


def _posix_readonly(target, slot):
    uid = os.geteuid()
    if uid == 0:
        return False
    for path in _protected_paths(target, slot):
        # The owner can chmod a read-only path; os.access also checks ACLs
        # and effective supplementary groups.
        if path.stat().st_uid == uid or os.access(path, os.W_OK, effective_ids=True):
            return False
    child = target
    for parent in target.parents:
        info = parent.stat()
        if info.st_uid == uid:
            return False
        if os.access(parent, os.W_OK | os.X_OK, effective_ids=True):
            if not (info.st_mode & stat.S_ISVTX) or child.stat().st_uid == uid:
                return False
        child = parent
    return True


@contextmanager
def _windows_access():
    import ctypes
    from ctypes import wintypes as win
    import win32api
    import win32con
    import win32security as security

    class GenericMapping(ctypes.Structure):
        _fields_ = [(name, win.DWORD) for name in ("read", "write", "execute", "all")]

    mapping = GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
    check = ctypes.WinDLL("advapi32", use_last_error=True).AccessCheck
    check.argtypes = [win.LPVOID, win.HANDLE, win.DWORD, ctypes.POINTER(GenericMapping), win.LPVOID,
                      ctypes.POINTER(win.DWORD), ctypes.POINTER(win.DWORD), ctypes.POINTER(win.BOOL)]
    check.restype = win.BOOL
    process = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_DUPLICATE | win32con.TOKEN_QUERY)
    try:
        token = security.DuplicateToken(process, security.SecurityImpersonation)
    finally:
        process.Close()
    try:
        admins = security.CreateWellKnownSid(security.WinBuiltinAdministratorsSid)
        reserved = {security.LookupPrivilegeValue(None, name) for name in
                    ("SeRestorePrivilege", "SeTakeOwnershipPrivilege", "SeDebugPrivilege", "SeTcbPrivilege")}
        privileges = security.GetTokenInformation(token, security.TokenPrivileges)
        if security.CheckTokenMembership(token, admins) or any(luid in reserved for luid, _ in privileges):
            raise ValueError("Managed Studio requires a non-administrator service account")

        def rights(path):
            sd = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
                                               security.OWNER_SECURITY_INFORMATION | security.GROUP_SECURITY_INFORMATION
                                               | security.DACL_SECURITY_INFORMATION)
            descriptor = ctypes.create_string_buffer(bytes(sd))
            privileges = ctypes.create_string_buffer(4096)
            length = win.DWORD(len(privileges))
            granted, allowed = win.DWORD(), win.BOOL()
            if not check(descriptor, int(token), 0x02000000, ctypes.byref(mapping), privileges,
                         ctypes.byref(length), ctypes.byref(granted), ctypes.byref(allowed)):
                raise ctypes.WinError(ctypes.get_last_error())
            return granted.value if allowed.value else 0

        yield rights
    finally:
        token.Close()


def _windows_readonly(target, slot):
    # FILE_WRITE_DATA/APPEND_DATA/WRITE_EA/WRITE_ATTRIBUTES, FILE_DELETE_CHILD,
    # DELETE, WRITE_DAC and WRITE_OWNER are actual ACL rights, independent of
    # the Windows read-only file attribute.
    replace = 0x00010000 | 0x00040000 | 0x00080000
    mutate = replace | 0x2 | 0x4 | 0x10 | 0x40 | 0x100
    with _windows_access() as rights:
        if any(rights(path) & mutate for path in _protected_paths(target, slot)):
            return False
        # Creating unrelated siblings cannot replace the bound target.
        return not any(rights(parent) & (replace | 0x40) for parent in target.parents)


def require_readonly(target, slot):
    try:
        safe = _windows_readonly(target, slot) if os.name == "nt" else _posix_readonly(target, slot)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Could not verify read-only access to the deployment target") from exc
    if not safe:
        raise ValueError("Service account can modify the deployment target or its selected source")

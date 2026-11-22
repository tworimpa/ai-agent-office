"""Exercise the fixed Windows host names exclusively on an opted-in disposable runner."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("OFFICE_SPLIT_ACL_ACCEPTANCE") != "1"
    or os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted",
    reason="requires the opted-in disposable GitHub-hosted Windows runner",
)

ROOT = Path(r"C:\ProgramData\AI-Agent-Office")
SERVICE = "AgentOfficeStudio"
HELPER = Path(__file__).resolve().parents[1] / "scripts" / "SplitSessionAcl.cs"


def _account(stack):
    import win32api
    import win32net
    import win32netcon
    import win32security as security

    suffix = uuid4().hex[:12]
    user, group = "aoU" + suffix, "aoG" + suffix
    password = "Aa1!" + secrets.token_hex(24)
    win32net.NetUserAdd(None, 1, dict(name=user, password=password, priv=win32netcon.USER_PRIV_USER,
                                     home_dir=None, comment="Disposable ACL test", script_path=None,
                                     flags=win32netcon.UF_SCRIPT | win32netcon.UF_NORMAL_ACCOUNT))
    stack.callback(win32net.NetUserDel, None, user)
    win32net.NetLocalGroupAdd(None, 1, dict(name=group, comment="Disposable ACL test"))
    stack.callback(win32net.NetLocalGroupDel, None, group)
    computer = win32api.GetComputerName()
    user_sid = security.LookupAccountName(None, computer + "\\" + user)[0]
    win32net.NetLocalGroupAddMembers(None, group, 0, [{"sid": user_sid}])
    group_sid = security.LookupAccountName(None, computer + "\\" + group)[0]
    token = security.LogonUser(user, computer, password, security.LOGON32_LOGON_INTERACTIVE,
                               security.LOGON32_PROVIDER_DEFAULT)
    stack.callback(token.Close)
    return token, security.ConvertSidToStringSid(group_sid)


def _service(stack, wrapper):
    import pywintypes
    import win32service as scm

    manager = scm.OpenSCManager(None, None, scm.SC_MANAGER_CONNECT | scm.SC_MANAGER_CREATE_SERVICE)
    stack.callback(scm.CloseServiceHandle, manager)
    try:
        existing = scm.OpenService(manager, SERVICE, scm.SERVICE_QUERY_STATUS)
    except pywintypes.error as exc:
        if exc.winerror != 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
            raise
    else:
        scm.CloseServiceHandle(existing)
        pytest.fail("Fixed service name already exists on acceptance runner")
    created = scm.CreateService(manager, SERVICE, SERVICE, scm.SERVICE_ALL_ACCESS, scm.SERVICE_WIN32_OWN_PROCESS,
                                scm.SERVICE_DISABLED, scm.SERVICE_ERROR_NORMAL, subprocess.list2cmdline([wrapper]),
                                None, 0, [], "NT SERVICE\\" + SERVICE, None)
    stack.callback(scm.CloseServiceHandle, created)
    stack.callback(scm.DeleteService, created)
    return created


@contextmanager
def _as_user(token):
    import win32security as security
    security.ImpersonateLoggedOnUser(token)
    try:
        yield
    finally:
        security.RevertToSelf()


def _check_rights(token, ledger, previous, granted):
    import pywintypes
    import win32service as scm

    with _as_user(token):
        manager = scm.OpenSCManager(None, None, scm.SC_MANAGER_CONNECT)
        try:
            def service_access(mask):
                scm.CloseServiceHandle(scm.OpenService(manager, SERVICE, mask))
            if granted:
                service_access(0x35)
                with ledger.open("r+b") as db:
                    assert db.read() == b"fixture"
                assert previous.read_bytes() == b"fixture"
            else:
                with pytest.raises(pywintypes.error) as denied:
                    service_access(0x35)
                assert denied.value.winerror == 5
                for path in (ledger, previous):
                    with pytest.raises(PermissionError):
                        path.read_bytes()
            with pytest.raises(pywintypes.error) as denied:
                service_access(scm.SERVICE_CHANGE_CONFIG)
            assert denied.value.winerror == 5
            with pytest.raises(PermissionError):
                with previous.open("r+b"):
                    pytest.fail("The previous slot became writable")
        finally:
            scm.CloseServiceHandle(manager)


def test_split_session_real_dacls_and_partial_recovery(tmp_path):
    import win32api
    import win32security as security
    import win32service as scm
    from agent_office.windows_binding import operator_sid

    assert not os.path.lexists(ROOT), "Refusing to use an existing host root"
    system = Path(win32api.GetSystemDirectory())
    with ExitStack() as stack:
        service = _service(stack, os.environ["OFFICE_NSSM_EXE"])
        token, group = _account(stack)
        ROOT.mkdir()  # Exclusive creation. Clean up only exact objects created below.
        stack.callback(ROOT.rmdir)
        subprocess.run([system / "icacls.exe", ROOT, "/inheritance:r", "/grant:r",
                        "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], check=True, timeout=15)
        dirs = ["release", "deploy", "deploy/slots", "deploy/slots/previous", "data", "data/tasks.sqlite3.locks"]
        files = ["release/release.sqlite3", "deploy/deployment.sqlite3", "deploy/slots/previous/main.py",
                 "data/tasks.sqlite3", "data/tasks.sqlite3.locks/studio-improvement-generation.lock"]
        for name in dirs:
            (ROOT / name).mkdir()
            stack.callback((ROOT / name).rmdir)
        for name in files:
            with (ROOT / name).open("xb") as fixture:
                fixture.write(b"fixture")
            stack.callback((ROOT / name).unlink)
        ledger, previous = ROOT / files[0], ROOT / files[2]

        def snapshot():
            dacl = security.DACL_SECURITY_INFORMATION
            descriptors = [security.GetFileSecurity(str(ROOT / name), dacl) for name in ["", *dirs, *files]]
            descriptors.append(scm.QueryServiceObjectSecurity(service, dacl))
            return [security.ConvertSecurityDescriptorToStringSecurityDescriptor(sd, 1, dacl) for sd in descriptors]

        def helper(log, actions, *, check=True):
            env = os.environ | {"AO_HELPER": str(HELPER), "AO_LOG": str(tmp_path / log),
                                "AO_GROUP": group, "AO_HUMAN": operator_sid()}
            script = ("$ErrorActionPreference='Stop'; Add-Type -Path $env:AO_HELPER; "
                      "$h=[SplitSessionAcl]::new($env:AO_LOG,$env:AO_GROUP,$env:AO_HUMAN); "
                      "try {" + actions + "} finally {$h.Dispose()}")
            return subprocess.run([system / "WindowsPowerShell/v1.0/powershell.exe", "-NoProfile",
                                   "-NonInteractive", "-Command", script], env=env, check=check, timeout=60)

        before = snapshot()
        _check_rights(token, ledger, previous, False)
        # Provision writes the release ACLs before inspecting unexpected deploy entries.
        unexpected = ROOT / "deploy" / "unexpected"
        with unexpected.open("xb"):
            pass
        try:
            assert helper("partial", "$h.Provision()", check=False).returncode != 0
            assert snapshot() != before
            helper("partial", "$h.Service($true); $h.Recover()")
            assert snapshot() == before
        finally:
            unexpected.unlink()
        helper("complete", "$h.Provision(); $h.Service($false)")
        try:
            _check_rights(token, ledger, previous, True)
        finally:
            helper("complete", "$h.Service($true); $h.Recover()")
        assert snapshot() == before
        _check_rights(token, ledger, previous, False)

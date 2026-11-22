"""Foreground processes for an administrator-authorized Windows deployment session."""

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time

from . import auto_deploy, release_store
from .models import validate_id
from .session_validator import SessionValidator, serve_session
from .windows_auto_task import RELEASE, TARGET, _prepare, _service_rights


SESSIONS = Path(r"C:\Program Files\AI-Agent-Office\SplitSession")
TRUSTED = {
    "S-1-5-18", "S-1-5-32-544",
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
}
LOCAL_SID = re.compile(r"S-1-5-21-\d+-\d+-\d+-\d+")


def _acl(path, *, writers=(), readers=None, parent=False):
    import win32security as sec

    info = path.lstat()
    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("Unexpected reparse point in deployment session path")
    descriptor = sec.GetNamedSecurityInfo(
        str(path), sec.SE_FILE_OBJECT, sec.OWNER_SECURITY_INFORMATION | sec.DACL_SECURITY_INFORMATION
    )
    allowed = TRUSTED.union(writers)
    if sec.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) not in allowed:
        raise ValueError("Unexpected deployment session file owner")
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        raise ValueError("Unrestricted deployment session directory")
    write = 0x500D0040 if parent else 0x500D0156
    for i in range(acl.GetAceCount()):
        header, mask, *rest = acl.GetAce(i)
        kind, flags = header
        if flags & 8 or kind == sec.ACCESS_DENIED_ACE_TYPE:  # Inherit-only ACE is not effective here
            continue
        if kind != sec.ACCESS_ALLOWED_ACE_TYPE:
            raise ValueError("Unsupported deployment session access rule")
        sid = sec.ConvertSidToStringSid(rest[-1])
        if sid == "S-1-3-4":  # OWNER RIGHTS, restricted above
            continue
        if mask & write and sid not in allowed:
            raise ValueError("Deployment session path can be replaced by another principal")
        if readers is not None and mask & 0x90000001 and sid not in allowed.union(readers):
            raise ValueError("Deployment validator channel is readable by another principal")


def _read(path):
    with path.open("rb") as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("Oversized operator session record")
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Invalid operator session record")
    return data


def _binding(directory):
    # Constrain the location before touching any path supplied by the caller.
    if directory.parent != SESSIONS or not re.fullmatch("[0-9a-f]{32}", directory.name):
        raise ValueError("Expected a fresh operator session in the fixed Program Files directory")
    for parent in reversed(directory.parents):
        _acl(parent, parent=True)
    _acl(directory)
    path = directory / "binding.json"
    _acl(path)
    fields = _read(path)
    for name in ("target_id", "release_id", "journal_id"):
        validate_id(fields[name])
    sids = [fields[key] for key in ("operator_sid", "validator_sid", "session_group_sid")]
    if not all(isinstance(sid, str) and LOCAL_SID.fullmatch(sid) for sid in sids) or len(set(sids)) != 3:
        raise ValueError("Expected distinct local validator, deployment operator and session group")
    release_store._check_image(fields["image"])
    if not isinstance(fields["runtime_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", fields["runtime_sha256"]):
        raise ValueError("Invalid operator runtime digest")
    if fields["enroll"] is not True:
        raise ValueError("Administrator enrollment is required for this session")
    return fields


def _check_principal(binding, role):
    import win32api
    import win32con
    import win32security as sec
    from .windows_binding import operator_sid

    if operator_sid() != binding[role + "_sid"]:
        raise ValueError("Current Windows identity differs from the authorized session role")
    account, _, _ = sec.LookupAccountName(None, win32api.GetComputerName() + "\\AgentOfficeOperator")
    if sec.ConvertSidToStringSid(account) != binding["operator_sid"]:
        raise ValueError("Session operator differs from the dedicated local account")
    token = sec.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        enabled = {sec.ConvertSidToStringSid(sid) for sid, flags in sec.GetTokenInformation(token, sec.TokenGroups)
                   if flags & 4 and not flags & 16}
    finally:
        token.Close()
    if (binding["session_group_sid"] in enabled) != (role == "operator"):
        raise ValueError("Operator session group is missing or shared with the validator")


def _channel(directory, binding, *, endpoint=False):
    channel = directory / "channel"
    options = dict(writers={binding["validator_sid"]},
                   readers={binding["operator_sid"], binding["session_group_sid"]})
    _acl(channel, **options)
    path = channel / "endpoint.json"
    if endpoint:
        _acl(path, **options)
    return path


def _serve(directory, binding):
    path = _channel(directory, binding)
    staged = path.with_suffix(".tmp")
    published = False

    def publish(endpoint):
        nonlocal published
        with staged.open("x", encoding="utf-8") as stream:
            _acl(staged, writers={binding["validator_sid"]},
                 readers={binding["operator_sid"], binding["session_group_sid"]})
            json.dump(endpoint, stream)
        # Windows refuses an existing destination. Readers see one complete record.
        staged.rename(path)
        published = True
        print("검증 세션 준비 완료", flush=True)

    try:
        serve_session(binding["target_id"], binding["release_id"], binding["journal_id"], binding["image"],
                      publish, session_open=lambda: (directory / "binding.json").is_file())
    finally:
        # The administrator also removes the session directory on teardown.
        if published:
            path.unlink(missing_ok=True)


class _WindowsValidator(SessionValidator):
    def __init__(self, endpoint, binding, directory):
        super().__init__(endpoint, binding)
        self.directory = directory
        self.descriptor = binding

    def ping(self):
        if _binding(self.directory) != self.descriptor:
            raise ValueError("The Windows deployment session ended")
        return super().ping()


def _operate(directory, binding, mirror_root):
    _service_rights()
    reviewed = auto_deploy.binding(RELEASE, TARGET, binding["image"])
    if ((reviewed["manager"], reviewed["service"]) != ("windows", "AgentOfficeStudio")
            or any(binding[key] != reviewed[key] for key in ("target_id", "release_id", "journal_id"))):
        raise ValueError("The service registration no longer matches the session")
    endpoint = _read(_channel(directory, binding, endpoint=True))
    validator = _WindowsValidator(endpoint, binding, directory)
    validator.ping()
    roles = {key: binding[key] for key in ("validator_sid", "operator_sid")}
    evidence = {**roles, "session_id": directory.name, "signer": endpoint["public_key"]}
    auto_deploy.claim_session(RELEASE, TARGET, reviewed, directory.name, roles)
    while _binding(directory) == binding:
        validator.ping()
        try:
            result = auto_deploy.run_once(RELEASE, TARGET, validator=validator, session_evidence=evidence,
                                          mirror_root=mirror_root)
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            result = {"state": "refused", "reason": str(exc)[:600]}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["state"] in {"disabled", "needs_operator"}:
            return
        # A closed foreground validator ends this authority even on an idle service.
        for _ in range(30):
            time.sleep(10)
            if not (directory / "binding.json").exists():
                return
            validator.ping()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["validator", "operator"])
    parser.add_argument("--session-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise ValueError("The split deployment session requires Windows")
    binding = _binding(args.session_dir)
    if args.role == "operator":
        runtime = SESSIONS.parent / "SplitRuntime" / binding["runtime_sha256"]
        _acl(runtime.parent)
        _acl(runtime)
        _prepare(tools=("git",), standalone=runtime)
    else:
        _prepare()
    _check_principal(binding, args.role)
    if args.role == "validator":
        _serve(args.session_dir, binding)
    else:
        with tempfile.TemporaryDirectory(prefix="office-operator-git-") as mirror:
            with release_store.use_session_object_store(RELEASE / "objects.git"):
                _operate(args.session_dir, binding, Path(mirror))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        sys.exit(str(error))

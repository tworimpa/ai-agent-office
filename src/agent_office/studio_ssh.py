"""Send a single journal-scoped studio request over an OpenSSH direct TCP channel."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from typing import BinaryIO

from .models import validate_id


_TIMEOUT = 12
_MAX_HEADER = 16 * 1024
# 200 snapshots of 8192 UTF-8 bytes, including JSON control-character escaping.
_MAX_RESPONSE = 12 * 1024 * 1024
_MAX_REQUEST = 128 * 1024


@dataclass(frozen=True)
class RemoteHost:
    alias: str
    port: int
    journal_id: str

    def __post_init__(self):
        if not isinstance(self.alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", self.alias):
            raise ValueError("SSH 별칭을 확인하세요.")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("원격 작업실 포트를 확인하세요.")
        validate_id(self.journal_id)


class RemoteUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("원격 작업실의 응답을 확인할 수 없습니다.")


def is_peer_route(method: str, route: str) -> bool:
    if method not in ("GET", "POST") or not isinstance(route, str):
        return False
    if route in ("/workspaces", "/runs"):
        return True
    pattern = r"/runs/[0-9a-f]{32}" if method == "GET" else r"/runs/[0-9a-f]{32}/stop"
    return re.fullmatch(pattern, route) is not None


def _ssh_command(host: RemoteHost) -> list[str]:
    if os.name == "nt":
        import ctypes

        system = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(system, len(system))
        if not 0 < length < len(system):
            raise RemoteUnavailable()
        executable = Path(system.value) / "OpenSSH" / "ssh.exe"
    else:
        executable = Path("/usr/bin/ssh")
    return [
        str(executable), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no", "-o", "ForwardAgent=no", "-o", "PermitLocalCommand=no",
        "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
        "-o", "ConnectionAttempts=1", "-o", "ConnectTimeout=6",
        "-W", f"127.0.0.1:{host.port}", "--", host.alias,
    ]


def _request_bytes(host: RemoteHost, method: str, route: str, payload: dict | None) -> bytes:
    if not is_peer_route(method, route):
        raise ValueError("지원하지 않는 원격 작업실 요청입니다.")
    address = f"127.0.0.1:{host.port}"
    headers = [f"{method} /api/peer/{host.journal_id}{route} HTTP/1.0",
               f"Host: {address}", "Connection: close"]
    body = b""
    if method == "POST":
        if not isinstance(payload, dict):
            raise ValueError("원격 요청에 JSON 객체가 필요합니다.")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > _MAX_REQUEST:
            raise ValueError("원격 요청의 크기를 확인하세요.")
        origin = "127.0.0.1" if host.port == 80 else address
        headers.extend((f"Origin: http://{origin}", "X-Agent-Office: studio",
                        "Content-Type: application/json", f"Content-Length: {len(body)}"))
    elif payload is not None:
        raise ValueError("조회 요청에는 본문을 사용할 수 없습니다.")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


def _reject_constant(_value):
    raise ValueError("Invalid JSON constant")


def _check_reply(status, data, journal_headers, journal_id, method, route, payload):
    if not isinstance(data, dict):
        raise RemoteUnavailable()
    if status >= 400:
        error = data.get("error")
        if not isinstance(error, dict) or not all(isinstance(error.get(k), str) for k in ("code", "message")):
            raise RemoteUnavailable()
        # These two refusals precede journal selection. Neither acknowledges a run.
        if not journal_headers and (status, error["code"]) in ((403, "PEER_DISABLED"), (409, "JOURNAL_MISMATCH")):
            return
    if journal_headers != [journal_id.encode("ascii")]:
        raise RemoteUnavailable()
    if status >= 400:
        return

    collection = route in ("/workspaces", "/runs")
    listing = method == "GET" and collection
    workspace = route == "/workspaces"
    expected_status = 200
    if method == "POST" and collection:
        expected_status = 201 if workspace else 202
    key = "workspace" if workspace else "run"
    rows = data.get(key + "s") if listing else [data.get(key)]
    if status != expected_status or not isinstance(rows, list):
        raise RemoteUnavailable()
    for row in rows:
        if not isinstance(row, dict):
            raise RemoteUnavailable()
        validate_id(row.get("id"))
        if workspace:
            if (not isinstance(row.get("name"), str) or not isinstance(row.get("folders"), list)
                    or not all(isinstance(path, str) for path in row["folders"])):
                raise RemoteUnavailable()
        elif not isinstance(row.get("state"), str):
            raise RemoteUnavailable()
        elif not listing:
            expected_id = payload.get("id") if route == "/runs" else route.split("/")[2]
            if row["id"] != expected_id:
                raise RemoteUnavailable()


def _read_response(stream: BinaryIO, journal_id: str, method: str, route: str, payload: dict | None) -> tuple[int, dict]:
    line = stream.readline(_MAX_HEADER + 1)
    status = re.fullmatch(rb"HTTP/1\.[01] (200|201|202|[45][0-9]{2})(?: [\t -~]*)?\r\n", line)
    if not status:
        raise RemoteUnavailable()
    remaining = _MAX_HEADER - len(line)
    headers = {}
    while remaining >= 2:
        line = stream.readline(remaining + 1)
        remaining -= len(line)
        if line == b"\r\n":
            break
        header = re.fullmatch(rb"([!#$%&'*+.^_|\x60~A-Za-z0-9-]+):([\t -~]*)\r\n", line)
        if remaining < 0 or not header:
            raise RemoteUnavailable()
        headers.setdefault(header[1].lower(), []).append(header[2].strip())
    else:
        raise RemoteUnavailable()

    lengths = headers.get(b"content-length", [])
    mimes = headers.get(b"content-type", [])
    if (len(lengths) != 1 or not re.fullmatch(rb"[0-9]{1,8}", lengths[0])
            or int(lengths[0]) > _MAX_RESPONSE or b"transfer-encoding" in headers
            or b"content-encoding" in headers or len(mimes) != 1
            or not re.fullmatch(rb"application/json(?:;[ \t]*charset=utf-8)?", mimes[0].lower())):
        raise RemoteUnavailable()
    length = int(lengths[0])
    body = stream.read(length)
    if len(body) != length:
        raise RemoteUnavailable()
    try:
        data = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        code = int(status[1])
        _check_reply(code, data, headers.get(b"x-agent-office-journal", []), journal_id, method, route, payload)
        return code, data
    except (ValueError, RecursionError):
        raise RemoteUnavailable() from None


def remote_request(host: RemoteHost, method: str, route: str, payload: dict | None = None) -> tuple[int, dict]:
    request = _request_bytes(host, method, route, payload)
    command = _ssh_command(host)
    try:
        with subprocess.Popen(command, cwd=Path(command[0]).parent,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as child:
            # Includes a blocked pipe write or a peer that stalls mid-frame.
            deadline = threading.Timer(_TIMEOUT, child.kill)
            deadline.daemon = True
            deadline.start()
            try:
                # Keep both directions of the direct TCP channel open until the
                # reply arrives. Content-Length already delimits the request.
                child.stdin.write(request)
                child.stdin.flush()
                # This channel serves one response. A complete pinned receipt is
                # authoritative even when the SSH bridge delays forwarding EOF.
                return _read_response(child.stdout, host.journal_id, method, route, payload)
            finally:
                deadline.cancel()
                child.kill()
    except (OSError, subprocess.SubprocessError):
        raise RemoteUnavailable() from None

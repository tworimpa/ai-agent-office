"""Exercise the same direct-stream HTTP exchange on Windows and Linux."""
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import time

import pytest

from agent_office import studio_ssh as ssh


JOURNAL = "0123456789abcdef" * 2
HOST = ssh.RemoteHost("lab-work_1", 8712, JOURNAL)


@pytest.mark.parametrize("alias,port,journal", [
    ("-oBatchMode=no", 22, JOURNAL), ("@host", 8766, JOURNAL), ("user@host", 80, JOURNAL),
    ("lab/one", 80, JOURNAL), ("lab\none", 80, JOURNAL), ("K", 80, JOURNAL),
    ("lab", True, JOURNAL), ("lab", 65536, JOURNAL), ("lab", 0, JOURNAL),
    ("lab", 80, JOURNAL.upper()), ("lab", 80, JOURNAL + "/runs"),
])
def test_host_configuration(alias, port, journal):
    with pytest.raises(ValueError):
        ssh.RemoteHost(alias, port, journal)


@pytest.mark.parametrize("method,route", [
    ("POST", "/runs/" + JOURNAL), ("GET", "/runs/" + JOURNAL + "/stop"),
    ("GET", "/api/runs"), ("POST", "/runs?host=other"), ("GET", "/../runs"),
    ("GET", "/runs/" + JOURNAL + "\r\nHost:other"), ("DELETE", "/workspaces"),
])
def test_only_fixed_routes_and_methods(method, route):
    assert not ssh.is_peer_route(method, route)
    with pytest.raises(ValueError):
        ssh.remote_request(HOST, method, route, {} if method == "POST" else None)


def frame(body, code=200, journal=JOURNAL):
    tag = f"X-Agent-Office-Journal: {journal}\r\n" if journal is not None else ""
    return (f"HTTP/1.0 {code} Reply\r\nContent-Type: application/json; charset=utf-8\r\n"
            f"{tag}Content-Length: {len(body)}\r\n\r\n").encode() + body


def mock_ssh(tmp_path, monkeypatch, source):
    script = tmp_path / "test ssh.py"
    read_request = r"""
import sys
from email.parser import BytesHeaderParser
stream = sys.stdin.buffer
first_line = stream.readline()
header_lines = []
while True:
    line = stream.readline()
    if not line:
        sys.exit(1)
    header_lines.append(line)
    if line == b"\r\n":
        break
raw_headers = b"".join(header_lines)
headers = BytesHeaderParser().parsebytes(raw_headers)
body = stream.read(int(headers.get("Content-Length", 0)))
"""
    script.write_text(read_request + source, encoding="utf-8")
    make_command = ssh._ssh_command
    monkeypatch.setattr(ssh, "_ssh_command",
                        lambda host: [sys._base_executable, str(script), *make_command(host)[1:]])


def test_native_process_sends_scoped_utf8_http_and_returns_peer_error(tmp_path, monkeypatch):
    capture = tmp_path / "sent"
    reply = frame('{"error":{"code":"REQUEST_CONFLICT","message":"요청 충돌"}}'.encode(), 409)
    mock_ssh(tmp_path, monkeypatch, f"""
import sys
from pathlib import Path
Path({str(capture)!r}).write_bytes(first_line + raw_headers + body)
sys.stdout.buffer.write({reply!r})
""")
    payload = {"id": JOURNAL, "task": "작은 테스트"}
    assert ssh.remote_request(HOST, "POST", "/runs", payload) == (409, {"error": {"code": "REQUEST_CONFLICT", "message": "요청 충돌"}})
    head, body = capture.read_bytes().split(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    assert lines[0] == f"POST /api/peer/{JOURNAL}/runs HTTP/1.0"
    assert "Host: 127.0.0.1:8712" in lines and "Origin: http://127.0.0.1:8712" in lines
    assert "Content-Type: application/json" in lines and "X-Agent-Office: studio" in lines
    assert f"Content-Length: {len(body)}" in lines and json.loads(body) == payload


def test_system_ssh_avoids_current_directory_and_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    Path("ssh.exe" if os.name == "nt" else "ssh").touch()
    argv = ssh._ssh_command(HOST)
    assert Path(argv[0]).is_absolute() and Path(argv[0]).parent != tmp_path
    assert argv[-2:] == ["--", HOST.alias]
    assert argv[argv.index("-W") + 1] == "127.0.0.1:8712"
    for option in ("StrictHostKeyChecking=yes", "BatchMode=yes", "PermitLocalCommand=no",
                   "ControlPath=none", "ForwardAgent=no"):
        assert option in argv


def test_legacy_projection_omits_only_bot_receipts_from_history_and_bundles():
    note = {"message": "operator data", "bots": "literal"}
    assignment = {"roles": {"developer": {"name": "Release developer"}}, "bots": {"developer": {"id": "release"}}}
    run = {"id": JOURNAL, "assignment": assignment}
    page = {"runs": [run], "next_cursor": "h-" + JOURNAL}
    bundle = {"room": page, "run": {"run": run}, "briefing": note}
    expected = {"id": JOURNAL, "assignment": {"roles": assignment["roles"]}}
    assert ssh.legacy_role_bot_reply(page) == {"runs": [expected], "next_cursor": page["next_cursor"]}
    projected = ssh.legacy_role_bot_reply(bundle)
    assert projected["room"]["runs"] == [expected] and projected["run"]["run"] == expected
    assert projected["briefing"] == note and "bots" in run["assignment"]


RUN_LIST = frame(b'{"runs":[]}')


@pytest.mark.parametrize("raw", [
    b"not HTTP", RUN_LIST.replace(b"200 Reply", b"302 Found"),
    RUN_LIST.replace(b"Content-Length", b"Bad Header"),
    RUN_LIST.replace(b"Length: 11", b"Length: 11\r\nContent-Length: 11"),
    RUN_LIST.replace(b"Length: 11", b"Length: 11\r\nTransfer-Encoding: chunked"),
    RUN_LIST.replace(b"application/json", b"text/html"),
    RUN_LIST.replace(b"Length: 11", b"Length: -2"),
    RUN_LIST.replace(b"Length: 11", b"Length: 9999999999"),
    RUN_LIST[:-1], frame(b'{"runs":[],"text":"\xff"}'), frame(b"[]"),
    frame(b'{"runs":[],"num":NaN}'), frame(b"{"), b"H" * 65536,
])
def test_malformed_peer_responses_are_unavailable(raw):
    with pytest.raises(ssh.RemoteUnavailable):
        ssh._read_response(BytesIO(raw), JOURNAL, "GET", "/runs", None)


def test_full_run_list_fits_limit():
    body = {"runs": [{"id": JOURNAL, "task": "\\" * 8192, "state": "PLAN_ONLY"} for _ in range(200)]}
    assert ssh._read_response(BytesIO(frame(json.dumps(body).encode())), JOURNAL, "GET", "/runs", None) == (200, body)


def test_hung_subprocess_is_killed_without_retry(tmp_path, monkeypatch):
    attempts = tmp_path / "attempts"
    mock_ssh(tmp_path, monkeypatch, f"""
import sys, time
with open({str(attempts)!r}, "a") as log:
    log.write("started\\n")
time.sleep(30)
""")
    monkeypatch.setattr(ssh, "_TIMEOUT", 2)
    started = time.monotonic()
    with pytest.raises(ssh.RemoteUnavailable):
        ssh.remote_request(HOST, "POST", "/runs", {})
    assert time.monotonic() - started < 10
    assert attempts.read_text().splitlines() == ["started"]


def test_ssh_failure_redacts_stderr_with_incomplete_response(tmp_path, monkeypatch):
    mock_ssh(tmp_path, monkeypatch, f"""
import sys
sys.stdout.buffer.write({b"HTTP/1.0"!r})
sys.stderr.write("remote-private-diagnostic")
sys.exit(255)
""")
    with pytest.raises(ssh.RemoteUnavailable) as error:
        ssh.remote_request(HOST, "GET", "/workspaces", None)
    assert "remote-private" not in str(error.value)

@pytest.mark.parametrize("method,route,request_body,code,response,journal", [
    ("GET", "/runs", None, 200, {"runs": []}, None),
    ("GET", "/runs", None, 200, {"runs": []}, "a" * 32),
    ("GET", "/runs", None, 200, {"runs": []}, JOURNAL + "\r\nX-Agent-Office-Journal: " + JOURNAL),
    ("GET", "/runs/" + JOURNAL, None, 404, {"error": {"code": "NOT_FOUND", "message": "missing"}}, None),
    ("GET", "/runs", None, 409, {"error": "oops"}, JOURNAL),
    ("GET", "/runs", None, 400, {"error": {"code": 1, "message": None}}, JOURNAL),
    ("GET", "/workspaces", None, 200, {"workspace": {}}, JOURNAL),
    ("GET", "/workspaces", None, 200, {"workspaces": [1]}, JOURNAL),
    ("POST", "/workspaces", {}, 201, {"workspace": {"id": JOURNAL, "name": "W", "folders": 1}}, JOURNAL),
    ("GET", "/runs", None, 201, {"runs": []}, JOURNAL),
    ("POST", "/runs", {"id": JOURNAL}, 202, {"run": {"id": "a" * 32, "state": "RUNNING"}}, JOURNAL),
    ("GET", "/runs/" + JOURNAL, None, 200, {"run": {"id": "a" * 32, "state": "RUNNING"}}, JOURNAL),
    ("POST", "/runs/" + JOURNAL + "/stop", {}, 200, {"run": {"id": JOURNAL}}, JOURNAL),
])
def test_peer_identity_status_and_envelope(method, route, request_body, code, response, journal):
    wire = frame(json.dumps(response).encode(), code, journal)
    with pytest.raises(ssh.RemoteUnavailable):
        ssh._read_response(BytesIO(wire), JOURNAL, method, route, request_body)


def test_tagged_missing_run_and_untagged_disabled_peer():
    missing = {"error": {"code": "NOT_FOUND", "message": "missing"}}
    wire = frame(json.dumps(missing).encode(), 404)
    assert ssh._read_response(BytesIO(wire), JOURNAL, "GET", "/runs/" + JOURNAL, None) == (404, missing)
    refusal = {"error": {"code": "PEER_DISABLED", "message": "disabled"}}
    wire = frame(json.dumps(refusal).encode(), 403, journal=None)
    assert ssh._read_response(BytesIO(wire), JOURNAL, "GET", "/runs", None) == (403, refusal)


def test_complete_receipt_finishes_even_if_forwarding_channel_stays_open(tmp_path, monkeypatch):
    data = {"run": {"id": JOURNAL, "state": "RUNNING"}}
    reply = frame(json.dumps(data).encode(), 202)
    mock_ssh(tmp_path, monkeypatch, f"""
import sys, threading, time
eof = threading.Event()
def watch_input():
    if not stream.read(1):
        eof.set()
threading.Thread(target=watch_input, daemon=True).start()
if eof.wait(0.2):
    sys.exit(255)
sys.stdout.buffer.write({reply!r})
sys.stdout.buffer.flush()
time.sleep(30)
""")
    monkeypatch.setattr(ssh, "_TIMEOUT", 8)
    started = time.monotonic()
    assert ssh.remote_request(HOST, "POST", "/runs", {"id": JOURNAL}) == (202, data)
    assert time.monotonic() - started < 6

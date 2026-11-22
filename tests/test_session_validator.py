from dataclasses import asdict
from pathlib import Path
import socket
import threading
import time

import pytest

from agent_office import policy_github as github, session_validator as session


SHA, BASE = "a" * 40, "b" * 40
IMAGE = "sha256:" + "c" * 64
BINDING = dict(target_id="1" * 32, release_id="2" * 32, journal_id="3" * 32, image=IMAGE)
REQUIRED = tuple(sorted(name for _, jobs in github.WORKFLOWS.values() for name in jobs))
PROOF = github.Attestation(24, "d" * 40, SHA, "e" * 40, (18, 19), REQUIRED, (1, 1), "f" * 40)
REPORT = dict(sha=SHA, image=IMAGE, passed=True, output="ok", exit_code=0)


class StopServer(Exception):
    pass


def test_public_socket_round_trip_binds_operator_mirror_and_refreshes(monkeypatch):
    calls = []
    endpoint_ready = threading.Event()
    endpoint = {}
    server_errors = []

    def mirror(directory):
        folder = Path(directory)
        assert folder.is_dir()
        calls.append(("mirror", folder))
        return folder / "auto-main"

    def attest(repository, sha, base, mirror):
        calls.append(("attest", repository, sha, base, mirror))
        return PROOF

    def validate(git, sha, image):
        calls.append(("validate", git, sha, image))
        return REPORT

    def ready(value):
        endpoint.update(value)
        endpoint_ready.set()

    original_handle = session._Server.handle

    def handle(server, connection):
        original_handle(server, connection)
        if sum(row[0] == "attest" for row in calls) == 3:
            raise StopServer()

    def serve():
        try:
            session.serve_session(**BINDING, on_ready=ready)
        except StopServer:
            pass
        except Exception as exc:
            server_errors.append(exc)
            endpoint_ready.set()

    monkeypatch.setattr(session.auto_deploy, "_mirror", mirror)
    monkeypatch.setattr(github, "attest", attest)
    monkeypatch.setattr(session.release_validation, "validate_candidate", validate)
    monkeypatch.setattr(session._Server, "handle", handle)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert endpoint_ready.wait(5) and not server_errors
    assert endpoint.keys() == {"port", "capability", "public_key"}
    client = session.SessionValidator(endpoint, BINDING)
    bad = session.SessionValidator(endpoint | {"capability": "0" * 64}, BINDING)
    with pytest.raises(session.SessionProtocolError):
        bad.validate(SHA, BASE, IMAGE)
    assert client.ping() is True
    assert not calls
    # A direct validate request performs its own canonical check before Docker.
    assert client.validate(SHA, BASE, IMAGE) == REPORT
    for _ in range(2):
        assert client.attest(github.REPOSITORY, SHA, BASE, Path("caller-private")) == PROOF
    thread.join(5)
    assert not thread.is_alive() and not server_errors
    folder = calls[0][1]
    assert not folder.exists()
    assert [row[0] for row in calls] == ["mirror", "attest", "validate", "mirror", "attest", "mirror", "attest"]
    assert calls[1] == calls[4] == calls[6] == ("attest", github.REPOSITORY, SHA, BASE, folder / "auto-main")
    assert calls[2] == ("validate", folder / "auto-main" / ".git", SHA, IMAGE)


@pytest.fixture
def server(tmp_path, monkeypatch):
    operator = session._Server(BINDING, tmp_path)
    monkeypatch.setattr(session.auto_deploy, "_mirror", lambda _: pytest.fail("unexpected operator work"))
    return operator


def request(**updates):
    return dict(BINDING, repository=github.REPOSITORY, sha=SHA, base=BASE, nonce="4" * 64,
                operation="validate", expires=int(time.time()) + 15) | updates


def exchange(server, req, capability=None):
    a, b = socket.socketpair()
    with a, b:
        tag = session._mac(server.capability if capability is None else capability, req)
        session._send(a, {"request": req, "mac": tag}, session._REQUEST_LIMIT)
        server.handle(b)
        return session._receive(a, session._REPLY_LIMIT, 2)


@pytest.mark.parametrize("update", [
    {"repository": "other/repo"}, {"operation": "pull"},
    {"sha": "-p"}, {"base": "HEAD"}, {"nonce": ""}, {"expires": 0}, {"expires": 2**60},
    {"mirror": "/elsewhere"}, {"command": ["other"]}, {"proxy": "https://example.org"},
])
def test_server_rejects_out_of_scope_before_tools(server, update):
    with pytest.raises(ValueError):
        exchange(server, request(**update))


@pytest.mark.parametrize("update", [{"target_id": "9" * 32}, {"journal_id": "9" * 32},
                                    {"release_id": "9" * 32}, {"image": "sha256:" + "9" * 64}])
def test_binding_failure_returns_signed_refusal_before_tools(server, update):
    reply = exchange(server, request(**update))
    assert reply["payload"]["result"] == "unauthorized"
    server.key.public_key().verify(bytes.fromhex(reply["signature"]),
                                   session._DOMAIN + b"reply\0" + session._encode(reply["payload"]))


def test_session_unavailable_if_listener_has_gone(server):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = dict(port=sock.getsockname()[1], public_key=server.key.public_key().public_bytes_raw().hex(),
                        capability=server.capability.hex())
        with pytest.raises(session.SessionUnavailable):
            session.SessionValidator(endpoint, BINDING).ping()


def test_missing_proof_refuses_docker_and_request_replay(server, monkeypatch):
    req = request()
    monkeypatch.setattr(session.auto_deploy, "_mirror", lambda path: path)
    monkeypatch.setattr(session.release_validation, "validate_candidate", lambda *_: pytest.fail("Docker"))

    def refused(*args):
        raise ValueError("private operator error")

    monkeypatch.setattr(github, "attest", refused)
    reply = exchange(server, req)
    assert reply["payload"]["result"] is None and "private" not in str(reply)
    with pytest.raises(ValueError):
        exchange(server, req)
    assert exchange(server, request(nonce="8" * 64), b"X" * 32)["payload"]["result"] == "unauthorized"


@pytest.mark.parametrize("wire_length", [0, session._REQUEST_LIMIT + 1, 2**32 - 1])
def test_server_drops_oversized_frame_without_reading_body(server, wire_length):
    a, b = socket.socketpair()
    with a, b, pytest.raises(ValueError):
        a.sendall(wire_length.to_bytes(4, "big"))
        server.handle(b)


@pytest.mark.parametrize("alter", ["wrong_key", "nonce", "operation", "target_id", "release_id", "journal_id",
                                   "sha", "base", "image", "expired", "future", "tamper", "missing_check"])
def test_consumer_rejects_tampered_expired_or_unbound_evidence(server, monkeypatch, alter):
    endpoint = dict(port=12345, capability=server.capability.hex(),
                    public_key=server.key.public_key().public_bytes_raw().hex())
    client = session.SessionValidator(endpoint, BINDING)
    captured = {}
    conn, peer = socket.socketpair()
    with peer:
        monkeypatch.setattr(socket, "create_connection", lambda *_, **kwargs: conn)
        monkeypatch.setattr(session, "_send", lambda _, frame, limit: captured.update(frame))

        def receive(*_):
            req = dict(captured["request"])
            assert endpoint["capability"] not in str(captured)
            result = asdict(PROOF)
            for key in ("check_names", "attempts", "run_ids"):
                result[key] = list(result[key])
            expiry = int(time.time()) + 15
            if alter in req:
                req[alter] = "changed"
            if alter == "expired":
                expiry = 1
            if alter == "future":
                expiry += 600
            if alter == "missing_check":
                result["check_names"].pop()
            payload = dict(request=req, expires=expiry, result=result)
            key = session.Ed25519PrivateKey.generate() if alter == "wrong_key" else server.key
            signature = key.sign(session._DOMAIN + b"reply\0" + session._encode(payload)).hex()
            if alter == "tamper":
                result["pr"] += 1
            return dict(payload=payload, signature=signature)

        monkeypatch.setattr(session, "_receive", receive)
        with pytest.raises(ValueError):
            client.attest(github.REPOSITORY, SHA, BASE, "other")

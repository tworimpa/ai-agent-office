"""Foreground, operator-launched validation for one target and one immutable image."""

from dataclasses import asdict
import hmac
import json
import os
import re
import secrets
import socket
import tempfile
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import auto_deploy, policy_github, release_store, release_validation
from .models import validate_id


_HOST = "127.0.0.1"
_REQUEST_LIMIT = 2048
_REPLY_LIMIT = 128 * 1024
_TTL = 30
_IO_TIMEOUT = 3
# Multiple bounded gh/Git subprocesses and the fixed container may run in sequence.
_JOB_TIMEOUT = 30 * 60
_DOMAIN = b"agent-office/session-validator/v1\0"


class SessionUnavailable(OSError):
    """No complete response from the foreground validator."""


class SessionProtocolError(ValueError):
    """Authentication, protocol or enrolled binding verification failed."""


def _check(condition):
    if not condition:
        raise SessionProtocolError("Invalid session validation message or binding")


def _hex(value, length):
    _check(isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value))
    return value


def _binding(target_id, release_id, journal_id, image):
    release_store._check_image(image)
    return dict(target_id=validate_id(target_id), release_id=validate_id(release_id),
                journal_id=validate_id(journal_id), image=image)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def _receive(connection, limit, timeout):
    deadline = time.monotonic() + timeout

    def read_exact(count):
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Session validation timed out")
            connection.settimeout(remaining)
            chunk = connection.recv(count - len(data))
            if not chunk:
                raise SessionUnavailable("Session validator disconnected")
            data.extend(chunk)
        return data

    size = int.from_bytes(read_exact(4), "big")
    _check(0 < size <= limit)
    try:
        value = json.loads(read_exact(size))
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise SessionProtocolError("Invalid session validation frame") from exc
    _check(type(value) is dict)
    return value


def _send(connection, value, limit):
    data = _encode(value)
    _check(len(data) <= limit)
    connection.settimeout(_IO_TIMEOUT)
    connection.sendall(len(data).to_bytes(4, "big") + data)


def _fresh(expires):
    now = int(time.time())
    _check(type(expires) is int and now <= expires <= now + _TTL)


def _mac(capability, request):
    return hmac.digest(capability, _DOMAIN + b"request\0" + _encode(request), "sha256").hex()


def _proof(result, sha):
    _check(isinstance(result, dict) and result.keys() == policy_github.Attestation.__dataclass_fields__.keys())
    _check(result["merge"] == sha and type(result["pr"]) is int and result["pr"] > 0)
    for name in ("head", "merge", "tested_merge", "tree"):
        _hex(result[name], 40)
    for name in ("run_ids", "attempts"):
        _check(type(result[name]) is list and len(result[name]) == len(policy_github.WORKFLOWS)
               and all(type(v) is int and v > 0 for v in result[name]))
    names = sorted(name for _, jobs in policy_github.WORKFLOWS.values() for name in jobs)
    _check(result["check_names"] == names and len(set(result["run_ids"])) == len(result["run_ids"]))
    return policy_github.Attestation(**(result | {
        key: tuple(result[key]) for key in ("run_ids", "attempts", "check_names")}))


class SessionValidator:
    def __init__(self, endpoint, binding):
        _check(isinstance(endpoint, dict) and set(endpoint) == {"port", "public_key", "capability"})
        self.port = endpoint["port"]
        _check(type(self.port) is int and 0 < self.port < 65536)
        self._public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(_hex(endpoint["public_key"], 64)))
        self._capability = bytes.fromhex(_hex(endpoint["capability"], 64))
        self._binding = _binding(*(binding[k] for k in ("target_id", "release_id", "journal_id", "image")))

    def _call(self, operation, sha=None, base=None):
        if operation != "ping":
            _hex(sha, 40)
            _hex(base, 40)
        request = dict(**self._binding, repository=policy_github.REPOSITORY, operation=operation,
                       sha=sha, base=base, nonce=secrets.token_hex(32),
                       expires=int(time.time()) + _TTL)
        try:
            with socket.create_connection((_HOST, self.port), timeout=_IO_TIMEOUT) as conn:
                _send(conn, {"request": request, "mac": _mac(self._capability, request)}, _REQUEST_LIMIT)
                reply = _receive(conn, _REPLY_LIMIT, _IO_TIMEOUT if operation == "ping" else _JOB_TIMEOUT)
        except OSError as exc:
            raise SessionUnavailable("Operator validation session is unavailable") from exc
        _check(reply.keys() == {"payload", "signature"})
        payload = reply["payload"]
        try:
            signature = bytes.fromhex(_hex(reply["signature"], 128))
            self._public.verify(signature, _DOMAIN + b"reply\0" + _encode(payload))
        except (InvalidSignature, RecursionError) as exc:
            raise SessionProtocolError("Invalid session validator signature") from exc
        _check(type(payload) is dict and payload.keys() == {"request", "expires", "result"}
               and payload["request"] == request)
        _fresh(payload["expires"])
        if payload["result"] == "unauthorized":
            raise SessionProtocolError("Operator session authentication or binding changed")
        if payload["result"] is None:
            raise ValueError("Operator session refused validation")
        return payload["result"]

    def ping(self):
        _check(self._call("ping") is True)
        return True

    def attest(self, repository, sha, base, mirror):
        # The caller's filesystem and repository configuration never cross the session boundary.
        _check(repository == policy_github.REPOSITORY)
        return _proof(self._call("attest", sha, base), sha)

    def validate(self, sha, base, image):
        _check(image == self._binding["image"])
        report = self._call("validate", sha, base)
        _check(type(report) is dict and report.keys() == {"sha", "image", "passed", "output", "exit_code"}
               and report["sha"] == sha and report["image"] == image and type(report["passed"]) is bool
               and isinstance(report["output"], str) and len(report["output"]) <= release_validation.MAX_OUTPUT
               and (report["exit_code"] is None or type(report["exit_code"]) is int)
               and (not report["passed"] or report["exit_code"] == 0))
        return report


class _Server:
    def __init__(self, binding, directory):
        self.binding = binding
        self.directory = directory
        self.key = Ed25519PrivateKey.generate()
        self.capability = secrets.token_bytes(32)
        self.used = {}

    def handle(self, conn):
        frame = _receive(conn, _REQUEST_LIMIT, _IO_TIMEOUT)
        _check(frame.keys() == {"request", "mac"})
        request = frame["request"]
        _check(type(request) is dict and request.keys() == self.binding.keys() |
               {"repository", "operation", "sha", "base", "nonce", "expires"})
        authenticated = hmac.compare_digest(_hex(frame["mac"], 64), _mac(self.capability, request))
        if not authenticated or any(request[k] != v for k, v in self.binding.items()):
            self.reply(conn, request, "unauthorized")
            return
        _check(request["repository"] == policy_github.REPOSITORY
               and request["operation"] in ("ping", "attest", "validate"))
        _fresh(request["expires"])
        nonce = _hex(request["nonce"], 64)
        now = int(time.time())
        self.used = {key: expiry for key, expiry in self.used.items() if expiry >= now}
        _check(nonce not in self.used and len(self.used) < 32)
        self.used[nonce] = request["expires"]
        if request["operation"] == "ping":
            _check(request["sha"] is None and request["base"] is None)
            self.reply(conn, request, True)
            return
        sha, base = _hex(request["sha"], 40), _hex(request["base"], 40)
        try:
            mirror = auto_deploy._mirror(self.directory)
            # Attest even for a standalone validate request, and refresh on every final attest.
            proof = policy_github.attest(policy_github.REPOSITORY, sha, base, mirror)
            if request["operation"] == "attest":
                result = asdict(proof)
            else:
                result = release_validation.validate_candidate(mirror / ".git", sha, self.binding["image"])
        except (OSError, RuntimeError, ValueError):
            # Keep CLI errors and operator-local paths out of the service account's journal.
            result = None
        self.reply(conn, request, result)

    def reply(self, conn, request, result):
        payload = dict(request=request, result=result, expires=int(time.time()) + _TTL)
        signature = self.key.sign(_DOMAIN + b"reply\0" + _encode(payload)).hex()
        _send(conn, {"payload": payload, "signature": signature}, _REPLY_LIMIT)


def serve_session(target_id, release_id, journal_id, image, on_ready, *, session_open=None):
    """Pass the session endpoint to the trusted launcher, then serve until interrupted."""
    binding = _binding(target_id, release_id, journal_id, image)
    with tempfile.TemporaryDirectory(prefix="office-session-") as directory, socket.socket() as sock:
        server = _Server(binding, directory)
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((_HOST, 0))
        sock.listen(4)
        if session_open is not None:
            sock.settimeout(1)
        on_ready(dict(port=sock.getsockname()[1], public_key=server.key.public_key().public_bytes_raw().hex(),
                      capability=server.capability.hex()))
        while session_open is None or session_open():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            with conn:
                if session_open is not None and not session_open():
                    return
                try:
                    server.handle(conn)
                except (OSError, ValueError, RecursionError):
                    continue

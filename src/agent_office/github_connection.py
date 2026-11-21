"""Host-owned GitHub connection definitions and TLS-only REST transport."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import re
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class GitHubError(RuntimeError):
    """Safe, enumerated error code. Never include remote bodies or credential paths."""

    def __init__(self, code, *, status=0, retry_after=0):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)


def positive_id(value):
    if type(value) is not int or value < 1 or value > 2**63 - 1:
        raise GitHubError("invalid_identifier")
    return value


def repository_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}/[A-Za-z0-9_.-]{1,100}", value):
        raise GitHubError("invalid_repository")
    if value.split("/")[1] in {".", ".."}:
        raise GitHubError("invalid_repository")
    return value


def _origin(value, *, proxy=False):
    if not isinstance(value, str) or not re.fullmatch(r"https?://[a-zA-Z0-9.-]+(?::[0-9]{1,5})?/?", value):
        raise GitHubError("invalid_origin")
    parsed = urlsplit(value.lower().rstrip("/"))
    try:
        port = parsed.port
    except ValueError:
        raise GitHubError("invalid_origin") from None
    if (not proxy and parsed.scheme != "https") or port == 0 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in parsed.hostname.split(".")
    ):
        raise GitHubError("invalid_origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _host_file(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise GitHubError("invalid_host_path")
    return Path(value)


@dataclass(frozen=True)
class GitHubConnection:
    id: str
    name: str
    kind: str
    web_url: str
    api_url: str
    app_id: int = 0
    installation_id: int = 0
    private_key_file: Path | None = field(default=None, repr=False)
    ca_file: Path | None = field(default=None, repr=False)
    proxy: str = field(default="system", repr=False)
    public_repositories: tuple[str, ...] = ()
    owner_journal: str | None = None

    @property
    def identity(self):
        key = [self.api_url, self.web_url, self.app_id, self.installation_id]
        return hashlib.sha256(json.dumps(key).encode()).hexdigest()

    @classmethod
    def parse(cls, row):
        allowed = set(cls.__dataclass_fields__)
        if not isinstance(row, dict) or set(row) - allowed:
            raise GitHubError("invalid_configuration")
        name_id, kind = row.get("id"), row.get("kind")
        if not isinstance(name_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name_id):
            raise GitHubError("invalid_connection_id")
        name = row.get("name", name_id)
        if not isinstance(name, str) or not 1 <= len(name) <= 100 or not name.isprintable():
            raise GitHubError("invalid_connection_name")
        web = _origin(row.get("web_url", "https://github.com"))
        if kind == "cloud" and web == "https://github.com":
            api = "https://api.github.com"
        elif kind == "residency" and re.fullmatch(r"https://[a-z0-9-]+\.ghe\.com", web):
            api = web.replace("https://", "https://api.", 1)
        elif kind == "ghes":
            api = web + "/api/v3"
        else:
            raise GitHubError("invalid_github_host")
        if row.get("api_url", api) != api:
            raise GitHubError("invalid_api_root")
        app_id = installation = 0
        key = None
        if any(k in row for k in ("app_id", "installation_id", "private_key_file")):
            app_id = positive_id(row.get("app_id"))
            installation = positive_id(row.get("installation_id"))
            key = _host_file(row.get("private_key_file"))
        repos = row.get("public_repositories", [])
        if not isinstance(repos, list) or len(repos) > 100 or (repos and key):
            raise GitHubError("invalid_repository_list")
        proxy = row.get("proxy", "system")
        if proxy not in ("system", "direct"):
            proxy = _origin(proxy, proxy=True)
        ca = _host_file(row["ca_file"]) if "ca_file" in row else None
        owner = row.get("owner_journal")
        if owner is not None and (not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner)):
            raise GitHubError("invalid_owner_journal")
        return cls(name_id, name, kind, web, api, app_id, installation, key, ca, proxy,
                   tuple(repository_name(repo) for repo in repos), owner)

    def public(self):
        return {**{k: getattr(self, k) for k in ("id", "name", "kind", "web_url", "api_url")},
                "auth": "app" if self.app_id else "anonymous"}

    def jwt(self, now):
        def encode(data):
            return base64.urlsafe_b64encode(data).rstrip(b"=")
        header = encode(b'{"alg":"RS256","typ":"JWT"}')
        claims = encode(json.dumps({"iat": int(now)-60, "exp": int(now)+540, "iss": str(self.app_id)}).encode())
        signed = header + b"." + claims
        try:
            with self.private_key_file.open("rb") as stream:
                data = stream.read(32769)
            if len(data) > 32768:
                raise ValueError()
            key = serialization.load_pem_private_key(data, password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise ValueError()
            signature = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        except (OSError, TypeError, ValueError, UnsupportedAlgorithm):
            raise GitHubError("app_key_unavailable") from None
        return (signed + b"." + encode(signature)).decode()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(repr=False)
class Response:
    status: int
    headers: dict
    data: object


class GitHubTransport:
    def __init__(self, connection):
        self.connection = connection
        self.opener = None

    def _open(self):
        cfg = self.connection
        try:
            tls = ssl.create_default_context()
            if cfg.ca_file:
                tls.load_verify_locations(cafile=str(cfg.ca_file))
        except (OSError, ValueError):
            raise GitHubError("ca_unavailable") from None
        proxies = None if cfg.proxy == "system" else {} if cfg.proxy == "direct" else {"https": cfg.proxy}
        return build_opener(ProxyHandler(proxies), _NoRedirect(), HTTPSHandler(context=tls))

    def send(self, method, path, headers, body=None):
        if self.opener is None:
            self.opener = self._open()
        if not path.startswith("/") or path.startswith("//"):
            raise GitHubError("invalid_api_path")
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.connection.api_url + path, data=data, method=method, headers=headers)
        try:
            try:
                response = self.opener.open(req, timeout=12)
            except HTTPError as err:
                response = err
            with response:
                raw = response.read(8 * 1024**2 + 1)
                if len(raw) > 8 * 1024**2:
                    raise GitHubError("response_too_large")
                # Invalid JSON in an error response (e.g. an HTML proxy page) needn't mask the status.
                try:
                    parsed = json.loads(raw) if raw else None
                except (ValueError, RecursionError):
                    if 200 <= response.status < 300:
                        raise GitHubError("invalid_response") from None
                    parsed = None
                return Response(response.status, {k.lower(): v for k, v in response.headers.items()}, parsed)
        except (URLError, OSError, HTTPException) as exc:
            cause = getattr(exc, "reason", exc)
            code = ("tls_verification_failed" if isinstance(cause, ssl.SSLError) else
                    "dns_or_vpn_unavailable" if isinstance(cause, socket.gaierror) else
                    "connection_timeout" if isinstance(cause, TimeoutError) else "network_or_proxy_unavailable")
            raise GitHubError(code) from None


def check_status(result, now=None):
    if 200 <= result.status < 300 or result.status == 304:
        return
    status = result.status
    if 300 <= status < 400:
        raise GitHubError("redirect_refused", status=status)
    headers = result.headers
    message = result.data.get("message", "") if isinstance(result.data, dict) else ""
    if status == 429 or (status == 403 and ("retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"
                                          or "rate limit" in str(message).lower())):
        try:
            delay = max(1, int(headers.get("retry-after", 0)),
                        int(headers.get("x-ratelimit-reset", 0)) - int(time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            delay = 60
        raise GitHubError("rate_limited", status=status, retry_after=max(60, delay))
    codes = {401: "authentication_rejected", 403: "permission_denied", 404: "not_found",
             407: "proxy_authentication_required", 409: "conflict", 410: "unsupported_api_version",
             422: "validation_failed"}
    raise GitHubError(codes.get(status, "github_unavailable" if status >= 500 else "github_request_failed"), status=status)

def worker_env(env):
    """Supply CLI processes no Studio GitHub registry, token or GitHub App key."""
    excluded = {"GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                "GITHUB_APP_PRIVATE_KEY", "GH_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY_PATH",
                "GITHUB_PRIVATE_KEY", "GH_PRIVATE_KEY"}
    return {k: v for k, v in env.items() if k.upper() not in excluded
            and not k.upper().startswith("AGENT_OFFICE_GITHUB_")}

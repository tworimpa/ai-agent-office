"""GitHub App client used only on the Studio host. All remote operations have fixed routes."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .github_connection import GitHubConnection, GitHubError, GitHubTransport, check_status, positive_id, repository_name


class GitHubRegistry:
    """The HTTP API can select existing connections but cannot modify this registry."""

    def __init__(self, rows=()):
        self._clients = {}
        for row in rows:
            cfg = GitHubConnection.parse(row)
            if cfg.id in self._clients:
                raise GitHubError("duplicate_connection")
            self._clients[cfg.id] = GitHubClient(cfg)

    @classmethod
    def load(cls, path=None):
        path = path or os.environ.get("AGENT_OFFICE_GITHUB_CONFIG")
        if not path:
            return cls()
        try:
            if not Path(path).is_absolute():
                raise GitHubError("invalid_registry_path")
            with Path(path).open("rb") as stream:
                raw = stream.read(131073)
            if len(raw) > 131072:
                raise ValueError()
            cfg = json.loads(raw)
            if not isinstance(cfg, dict) or set(cfg) != {"connections"}:
                raise ValueError()
            rows = cfg["connections"]
            if not isinstance(rows, list) or len(rows) > 64:
                raise ValueError()
            return cls(rows)
        except (ValueError, OSError, RecursionError):
            raise GitHubError("invalid_registry") from None

    def connections(self):
        return [client.config.public() for client in self._clients.values()]

    def client(self, connection_id):
        try:
            return self._clients[connection_id]
        except (KeyError, TypeError):
            raise GitHubError("unknown_connection") from None


@dataclass
class GitHubPage:
    items: list[dict]
    next_page: int | None
    etag: str | None
    not_modified: bool = False


def _dict(data):
    if not isinstance(data, dict):
        raise GitHubError("invalid_response")
    return data


def _items(data):
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise GitHubError("invalid_response")
    return data


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise GitHubError("invalid_commit")
    return value


def _branch(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 250 or not value.isprintable()
            or value == "@" or value.startswith("-") or value.endswith(".") or ".." in value or "@{" in value
            or any(c in value for c in " ~^:?*[\\")
            or any(not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise GitHubError("invalid_branch")
    return value


def _repo(repo):
    return "/repos/" + repository_name(repo)


def _next_page(link, api_url, page):
    # GitHub may alias /repos/OWNER/REPO to /repositories/ID. Carry ONLY the
    # consecutive numeric cursor into our own fixed path, filters and origin.
    for item in link.split(","):
        if not re.search(r';\s*rel="next"(?:;|$)', item.strip()):
            continue
        match = re.match(r"\s*<([^>]+)>", item)
        if not match:
            raise GitHubError("invalid_pagination")
        try:
            url = urlsplit(match[1])
            root = urlsplit(api_url)
            target = parse_qs(url.query, max_num_fields=32).get("page")
        except ValueError:
            raise GitHubError("invalid_pagination") from None
        if (url.scheme != root.scheme or url.netloc.lower() != root.netloc.lower() or url.fragment
                or not url.path.startswith(root.path + "/") or target != [str(page + 1)]):
            raise GitHubError("invalid_pagination")
        return page + 1
    return None


class GitHubClient:
    def __init__(self, config, *, transport=None, clock=time.time):
        self.config = config
        self.identity = config.identity
        self.web_url = config.web_url
        self.app_id = config.app_id
        self._transport = transport or GitHubTransport(config)
        self._clock = clock
        self._lock = threading.RLock()
        self._token = None
        self._expires = 0.0
        self._app_slug = None
        self._version = None

    def _send(self, method, path, auth=None, body=None, etag=None):
        headers = {"User-Agent": "AI-Agent-Office", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if auth:
            headers["Authorization"] = "Bearer " + auth
        if body is not None:
            headers["Content-Type"] = "application/json"
        if etag:
            if not isinstance(etag, str) or not re.fullmatch(r'(?:W/)?"[A-Za-z0-9/+=:._-]{1,180}"', etag):
                raise GitHubError("invalid_etag")
            headers["If-None-Match"] = etag
        return self._transport.send(method, path, headers, body)

    def _server_version(self):
        with self._lock:
            if self.config.kind == "ghes" and self._version is None:
                meta = self._send("GET", "/meta")
                if meta.status in (401, 403) and self.app_id:
                    try:
                        check_status(meta, self._clock())
                    except GitHubError as error:
                        if error.code == "rate_limited":
                            raise
                        meta = self._send("GET", "/meta", self._access())
                check_status(meta)
                version = _dict(meta.data).get("installed_version")
                match = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:[-+][A-Za-z0-9.-]{1,64})?", version if isinstance(version, str) else "")
                if not match or tuple(map(int, match.groups()[:2])) < (3, 19):
                    raise GitHubError("unsupported_ghes_version")
                self._version = version
        return self._version

    def _access(self):
        if not self.app_id:
            return None
        with self._lock:
            now = self._clock()
            if self._token and now + 90 < self._expires:
                return self._token
            self._token = None
            token, expires, _ = self._mint({})
            self._token, self._expires = token, expires
            return token

    def _mint(self, scope):
        now = self._clock()
        path = f"/app/installations/{self.config.installation_id}/access_tokens"
        result = self._send("POST", path, self.config.jwt(now), scope)
        check_status(result, now)
        data = _dict(result.data)
        try:
            expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if expiry.utcoffset() is None:
                raise ValueError()
            expires = expiry.timestamp()
            token = data["token"]
            if not isinstance(token, str) or len(token) > 4096 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) or expires <= now + 5:
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
            raise GitHubError("invalid_installation_token") from None
        return token, expires, data

    @contextmanager
    def git_read_token(self, repo, expected_repo_id=None):
        if not self.app_id:
            yield None
            return
        row = self.repository(repo)
        if repository_name(row.get("full_name")).lower() != repo.lower():
            raise GitHubError("repository_identity_changed")
        rid = positive_id(row.get("id"))
        if expected_repo_id is not None and rid != positive_id(expected_repo_id):
            raise GitHubError("repository_identity_changed")
        token, _, result = self._mint({"repository_ids": [rid], "permissions": {"contents": "read"}})
        try:
            permissions = result.get("permissions")
            if (not isinstance(permissions, dict) or permissions.get("contents") != "read"
                    or any(k not in {"metadata", "contents"} or v != "read" for k,v in permissions.items())
                    or [r.get("id") for r in _items(result.get("repositories"))] != [rid]):
                raise GitHubError("fetch_scope_unconfirmed")
            yield token
        finally:
            response = self._send("DELETE", "/installation/token", token)
            if response.status != 401:
                check_status(response)

    def revoke(self):
        """Revoke the cached token at GitHub without minting one to revoke it."""
        with self._lock:
            token, self._token = self._token, None
            if token:
                result = self._send("DELETE", "/installation/token", token)
                if result.status != 401:
                    check_status(result)

    def _request(self, method, path, body=None, etag=None):
        self._server_version()
        if method != "GET" and not self.app_id:
            raise GitHubError("installation_required")
        token = self._access()
        result = self._send(method, path, token, body, etag)
        if token and result.status == 401:
            with self._lock:
                if self._token == token:
                    self._token = None
            if method == "GET":
                result = self._send(method, path, self._access(), body, etag)
        check_status(result, self._clock())
        return result

    def _page(self, path, page, *, params=None, field=None, etag=None):
        positive_id(page)
        query = urlencode({**(params or {}), "page": page, "per_page": 100})
        result = self._request("GET", path + "?" + query, etag=etag)
        if result.status == 304:
            return GitHubPage([], None, etag, True)
        data = _dict(result.data).get(field) if field else result.data
        items = _items(data)
        nxt = _next_page(result.headers.get("link", ""), self.config.api_url, page)
        return GitHubPage(items, nxt, result.headers.get("etag"))

    def _all(self, path, *, params=None, field=None):
        items, page = [], 1
        for _ in range(20):
            result = self._page(path, page, params=params, field=field)
            items.extend(result.items)
            if result.next_page is None:
                return items
            page = result.next_page
        raise GitHubError("pagination_limit")

    def diagnose(self):
        cfg = self.config
        info = {"kind": cfg.kind, "api_url": cfg.api_url, "web_url": cfg.web_url,
                "authentication": "installation" if cfg.app_id else "anonymous",
                "proxy": cfg.proxy if cfg.proxy in ("system", "direct") else "configured",
                "ca": "custom" if cfg.ca_file else "system", "version": None}
        try:
            info["version"] = self._server_version()
            if cfg.app_id:
                self._page("/installation/repositories", 1, field="repositories")
            elif cfg.kind != "ghes":
                self._request("GET", "/meta")
            return {**info, "ok": True, "code": "ok"}
        except GitHubError as error:
            return {**info, "ok": False, "code": error.code}

    def repositories(self):
        if self.app_id:
            rows = self._all("/installation/repositories", field="repositories")
        else:
            rows = [self.repository(repo) for repo in self.config.public_repositories]
        return [{"id": positive_id(r.get("id")), "full_name": repository_name(r.get("full_name")),
                 "default_branch": _branch(r.get("default_branch"))} for r in rows]

    def repository(self, repo):
        return _dict(self._request("GET", _repo(repo)).data)

    def issue_page(self, repo, *, labels=(), state="all", since=None, page=1, etag=None):
        if state not in ("all", "open", "closed"):
            raise GitHubError("invalid_issue_state")
        params = {"state": state, "sort": "updated", "direction": "asc"}
        if labels:
            params["labels"] = ",".join(labels)
        if since:
            params["since"] = since
        return self._page(_repo(repo) + "/issues", page, params=params, etag=etag)

    def issue(self, repo, number):
        return _dict(self._request("GET", f"{_repo(repo)}/issues/{positive_id(number)}").data)

    def timeline(self, repo, number):
        return self._all(f"{_repo(repo)}/issues/{positive_id(number)}/timeline")

    def comments(self, repo, number):
        return self._all(f"{_repo(repo)}/issues/{positive_id(number)}/comments")

    def comment_page(self, repo, number, *, since=None, page=1):
        return self._page(f"{_repo(repo)}/issues/{positive_id(number)}/comments", page,
                          params={"since": since} if since else None)

    def comment(self, repo, comment_id):
        return _dict(self._request("GET", f"{_repo(repo)}/issues/comments/{positive_id(comment_id)}").data)

    def add_comment(self, repo, number, body):
        return _dict(self._request("POST", f"{_repo(repo)}/issues/{positive_id(number)}/comments", {"body": body}).data)

    def update_comment(self, repo, comment_id, body):
        return _dict(self._request("PATCH", f"{_repo(repo)}/issues/comments/{positive_id(comment_id)}", {"body": body}).data)

    def add_labels(self, repo, number, labels):
        return _items(self._request("POST", f"{_repo(repo)}/issues/{positive_id(number)}/labels", {"labels": labels}).data)

    def remove_label(self, repo, number, label):
        if not isinstance(label, str) or label in ("", ".", "..") or "/" in label:
            raise GitHubError("invalid_label")
        self._request("DELETE", f"{_repo(repo)}/issues/{positive_id(number)}/labels/{quote(label, safe='')}")

    def permission(self, repo, login, expected_id=None):
        if not isinstance(login, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", login):
            return "none"
        path = f"{_repo(repo)}/collaborators/{login}/permission"
        try:
            result = _dict(self._request("GET", path).data)
            if expected_id is not None:
                user = result.get("user")
                if (not isinstance(user, dict) or user.get("id") != positive_id(expected_id)
                        or str(user.get("login", "")).lower() != login.lower() or user.get("type") != "User"):
                    return "none"
            return result.get("permission", "none")
        except GitHubError as exc:
            if exc.status == 404:
                return "none"
            raise

    def is_app_actor(self, event):
        if not self.app_id:
            return False
        user = event.get("actor") or event.get("user")
        if not isinstance(user, dict) or user.get("type") != "Bot":
            return False
        via = event.get("performed_via_github_app")
        if isinstance(via, dict) and "id" in via:
            return via["id"] == self.app_id
        with self._lock:
            if not self._app_slug:
                result = self._send("GET", "/app", self.config.jwt(self._clock()))
                check_status(result)
                app = _dict(result.data)
                if app.get("id") != self.app_id or not isinstance(app.get("slug"), str):
                    raise GitHubError("app_identity_mismatch")
                self._app_slug = app["slug"]
        return user.get("login") == self._app_slug + "[bot]"

    def pulls(self, repo, head_branch, base_branch):
        params = {"state": "all", "head": f"{repository_name(repo).split('/')[0]}:{_branch(head_branch)}",
                  "base": _branch(base_branch)}
        return self._all(_repo(repo) + "/pulls", params=params)

    def pull(self, repo, number):
        return _dict(self._request("GET", f"{_repo(repo)}/pulls/{positive_id(number)}").data)

    def reviews(self, repo, number):
        return self._all(f"{_repo(repo)}/pulls/{positive_id(number)}/reviews")

    def checks(self, repo, head_sha):
        return self._all(f"{_repo(repo)}/commits/{_sha(head_sha)}/check-runs", field="check_runs")

    def statuses(self, repo, head_sha):
        # Individual legacy status objects omit the SHA; the requested resource
        # already pins the full commit. Preserve an explicit conflicting SHA.
        return [{"sha": head_sha, **row} for row in self._all(f"{_repo(repo)}/commits/{_sha(head_sha)}/statuses")]

    def create_draft(self, repo, head, base, title, body):
        if not _branch(head).startswith("ao/") or head == base:
            raise GitHubError("invalid_candidate_branch")
        payload = {"head": head, "base": _branch(base), "title": title, "body": body,
                   "draft": True, "maintainer_can_modify": False}
        return _dict(self._request("POST", _repo(repo) + "/pulls", payload).data)

    def publish_candidate(self, repo, *, base_branch, base_sha, head_branch, files, title, body, expected_tree=None):
        from .github_publish import publish_candidate
        return publish_candidate(self, repo, base_branch, base_sha, head_branch, files, title, body, expected_tree)

    def branch_sha(self, repo, branch):
        path = f"{_repo(repo)}/git/ref/heads/{quote(_branch(branch), safe='/')}"
        result = _dict(self._request("GET", path).data)
        obj = _dict(result.get("object"))
        if result.get("ref") != "refs/heads/" + branch or obj.get("type") != "commit":
            raise GitHubError("branch_mismatch")
        return _sha(obj.get("sha"))

    def advance_candidate(self, repo, *, base_branch, head_branch, expected_head, pr_number, files, expected_tree, title):
        from .github_publish import advance_candidate
        return advance_candidate(self, repo, base_branch, head_branch, expected_head, pr_number, files, expected_tree, title)

    def read_commit(self, repo, sha):
        result = _dict(self._request("GET", f"{_repo(repo)}/git/commits/{_sha(sha)}").data)
        if result.get("sha") != sha:
            raise GitHubError("commit_mismatch")
        return result

    def commit_tree(self, repo, sha):
        return _sha(_dict(self.read_commit(repo, sha).get("tree")).get("sha"))

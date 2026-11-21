"""GitHub fixtures use only disposable keys and do not contact a provider."""
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import ssl
from urllib.error import URLError

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import pytest

from agent_office.github_api import GitHubClient, GitHubRegistry
from agent_office.github_connection import GitHubConnection, GitHubError, GitHubTransport, Response, _NoRedirect, worker_env


REPO = "owner/repo"


class FakeTransport:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.requests = []

    def send(self, method, path, headers, body=None):
        self.requests.append((method, path, headers, body))
        expected_method, expected_path, status, response, *extra = self.scripted.pop(0)
        assert method == expected_method
        assert path.split("?")[0] == expected_path
        return Response(status, extra[0] if extra else {}, response)


@pytest.fixture()
def host_key(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "fixture.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return path, key.public_key()


def connection(key_path, kind="cloud"):
    row = {"id": "fixture", "kind": kind, "app_id": 431, "installation_id": 12, "private_key_file": str(key_path)}
    if kind == "ghes":
        row["web_url"] = "https://git.example"
    return GitHubConnection.parse(row)


def test_hosts_registry_has_only_public_fields(tmp_path, host_key):
    path = tmp_path / "host.json"
    path.write_text(json.dumps({"connections": [
        {"id": "c", "kind": "cloud"},
        {"id": "r", "kind": "residency", "web_url": "https://tenant.ghe.com"},
        {"id": "s", "kind": "ghes", "web_url": "https://intranet:8443", "app_id": 431,
         "installation_id": 12, "private_key_file": str(host_key[0]), "ca_file": str(tmp_path/"CA"),
         "proxy": "http://proxy.example:3128"}]}))
    registry = GitHubRegistry.load(path)
    public = registry.connections()
    assert [r["api_url"] for r in public] == ["https://api.github.com", "https://api.tenant.ghe.com", "https://intranet:8443/api/v3"]
    assert public[-1]["auth"] == "app"
    assert not any(v in str(public) for v in (str(tmp_path), "431", "proxy.example", "private_key"))
    assert registry.client("s").identity != registry.client("r").identity
    assert worker_env({"A": "1", "gh_token": "x", "GITHUB_APP_PRIVATE_KEY": "x",
                       "AGENT_OFFICE_GITHUB_CONFIG": str(path)}) == {"A": "1"}


@pytest.mark.parametrize("row", [
    {"web_url": "https://x@corp.example"}, {"web_url": "http://corp.example"}, {"web_url": "https://corp.example/api/v3"},
    {"web_url": "https://corp.example?token=x"}, {"web_url": "https://corp.example/#x"},
    {"web_url": "https://example:65536"}, {"api_url": "https://different.example/api/v3"},
    {"proxy": "http://user:pass@proxy.example"}, {"token": "inline"}, {"private_key_file": "./local.pem"},
    {"kind": "residency", "web_url": "https://tenant.ghe.com.example"},
])
def test_reject_unpinned_or_inline_configuration(row):
    with pytest.raises(GitHubError):
        GitHubRegistry([{"id": "s", "kind": "ghes", **row}])


def token_response(value, expiry):
    return ("POST", "/app/installations/12/access_tokens", 201,
            {"token": value, "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
             "permissions": {"metadata": "read"}})

def grants(**permissions):
    return ("GET", "/app/installations/12", 200, {"id":12, "app_id":431, "permissions":permissions})


def scoped_response(value, expiry, repo_id, **permissions):
    method, path, status, body = token_response(value, expiry)
    return method, path, status, dict(body, repositories=[{"id":repo_id}], permissions={"metadata":"read", **permissions})


def test_signed_installation_token_refresh_401_revocation_and_no_write_replay(host_key):
    path, public = host_key
    now = [1700000000]
    issue = "/repos/owner/repo/issues/4"
    script = [token_response("first", now[0] + 3600), ("GET", issue, 200, {"id": 1}),
              ("GET", issue, 401, {"message": "sensitive echo"}), token_response("second", now[0] + 3600),
              ("GET", issue, 200, {"id": 1}), token_response("third", now[0] + 7200),
              ("GET", issue, 200, {"id": 1}), ("DELETE", "/installation/token", 204, None),
              grants(issues="write"), scoped_response("fourth", now[0] + 7200, 6, issues="write"),
              ("POST", issue + "/comments", 401, {"message": "fourth"})]
    wire = FakeTransport(script)
    client = GitHubClient(connection(path), clock=lambda: now[0], transport=wire)
    assert client.issue(REPO, 4)["id"] == 1
    jwt = wire.requests[0][2]["Authorization"].removeprefix("Bearer ")
    head, claims, sig = jwt.split(".")
    def unpad(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    public.verify(unpad(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(unpad(claims)) == {"iss": "431", "iat": now[0] - 60, "exp": now[0] + 540}
    assert client.issue(REPO, 4)["id"] == 1
    now[0] += 3520
    assert client.issue(REPO, 4)["id"] == 1
    client.revoke()
    with pytest.raises(GitHubError, match="^authentication_rejected$"):
        client.for_repository(6).add_comment(REPO, 4, "public result")
    assert not wire.scripted
    assert [wire.requests[i][2]["Authorization"] for i in (1, 4, 6, 7, 10)] == [
        "Bearer first", "Bearer second", "Bearer third", "Bearer third", "Bearer fourth"]


def test_ghes_protected_metadata_and_repository_listing(host_key):
    cfg = connection(host_key[0], kind="ghes")
    wire = FakeTransport([("GET", "/meta", 401, {}), token_response("temporary", 10000),
                          ("GET", "/meta", 200, {"installed_version": "3.19.7"}),
                          ("GET", "/installation/repositories", 200, {"repositories": []}),
                          ("GET", "/installation/repositories", 200, {"repositories": [
                              {"id": 42, "full_name": "user_code/repo", "default_branch": "trunk"}]})])
    client = GitHubClient(cfg, transport=wire, clock=lambda: 5000)
    status = client.diagnose()
    assert status["version"] == "3.19.7" and status["ok"] and status["authentication"] == "installation"
    assert client.repositories() == [{"id": 42, "full_name": "user_code/repo", "default_branch": "trunk"}]
    cfg = GitHubConnection.parse({"id": "s", "kind": "ghes", "web_url": "https://example.local"})
    old = GitHubClient(cfg, transport=FakeTransport([("GET", "/meta", 200, {"installed_version": "3.18.1"})]))
    assert old.diagnose()["code"] == "unsupported_ghes_version"


def test_numeric_cursor_never_follows_response_paths_and_preserves_filters():
    cfg = GitHubConnection.parse({"id": "public", "kind": "cloud"})
    path = "/repos/owner/repo/issues"
    wire = FakeTransport([("GET", path, 200, [{}], {"link": '<https://api.github.com/repositories/42/issues?page=2>; rel="next"'}),
                          ("GET", path, 200, [], {"etag": '"a"'}), ("GET", path, 304, {}),
                          ("GET", path, 200, [], {"link": '<https://evil.example/x?page=2>; rel="next"'})])
    client = GitHubClient(cfg, transport=wire)
    p = client.issue_page(REPO, labels=["a", "b"])
    assert p.next_page == 2
    last = client.issue_page(REPO, labels=["a", "b"], page=p.next_page)
    assert "labels=a%2Cb" in wire.requests[1][1] and last.next_page is None
    assert client.issue_page(REPO, etag=last.etag).not_modified
    with pytest.raises(GitHubError, match="invalid_pagination"):
        client.issue_page(REPO)
    with pytest.raises(GitHubError, match="invalid_etag"):
        client.issue_page(REPO, etag="x\nInjected: a")
    with pytest.raises(GitHubError, match="installation_required"):
        client.add_comment(REPO, 1, "ignored")
    assert not wire.scripted


def test_stable_actor_and_rate_limit_errors(host_key):
    path, _ = host_key
    wire = FakeTransport([("GET", "/app", 200, {"id": 431, "slug": "office-agent"}),
                          token_response("transient", 4000),
                          ("GET", "/repos/owner/repo/issues/10", 403, {"message": "private echo"},
                           {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "3590"})])
    client = GitHubClient(connection(path), transport=wire, clock=lambda: 3000)
    assert client.is_app_actor({"actor": {"type": "Bot"}, "performed_via_github_app": {"id": 431}})
    assert not client.is_app_actor({"user": {"type": "User"}, "performed_via_github_app": {"id": 431}})
    assert not client.is_app_actor({"user": {"type": "User", "login": "office-agent[bot]"}})
    assert client.is_app_actor({"user": {"type": "Bot", "login": "office-agent[bot]"}})
    assert not client.is_app_actor({"actor": {"type": "Bot", "login": "other[bot]"}})
    with pytest.raises(GitHubError) as err:
        client.issue(REPO, 10)
    assert err.value.code == "rate_limited" and err.value.retry_after == 590


def test_transport_tls_errors_and_redirects_are_sanitized():
    cfg = GitHubConnection.parse({"id": "s", "kind": "cloud"})
    http = GitHubTransport(cfg)
    class Broken:
        def open(self, req, timeout):
            raise URLError(ssl.SSLCertVerificationError("private proxy detail"))
    http.opener = Broken()
    assert _NoRedirect().redirect_request(None, None, 302, None, {}, "https://foreign.example") is None
    with pytest.raises(GitHubError, match="^tls_verification_failed$"):
        http.send("GET", "/meta", {})
    wire = FakeTransport([("GET", "/meta", 302, None, {"location": "https://elsewhere.example"})])
    assert GitHubClient(cfg, transport=wire).diagnose()["code"] == "redirect_refused"


def test_collaborator_permission_is_bound_to_stable_account_identity():
    cfg = GitHubConnection.parse({"id": "c", "kind": "cloud"})
    path = "/repos/owner/repo/collaborators/actor/permission"
    user = {"id": 1234, "login": "Actor", "type": "User"}
    wire = FakeTransport([("GET", path, 200, {"permission": "admin", "user": user})] * 2)
    client = GitHubClient(cfg, transport=wire)
    assert client.permission(REPO, "actor", 1234) == "admin"
    assert client.permission(REPO, "actor", 5678) == "none"


def test_legacy_status_inherits_only_queried_sha_when_row_omits_sha():
    head = "a" * 40
    path = "/repos/owner/repo/commits/" + head + "/statuses"
    rows = [{"id": 3, "context": "jenkins", "state": "success", "url": "https://api.github.com" + path},
            {"id": 2, "sha": "b"*40, "state": "failure", "context": "jenkins"}]
    wire = FakeTransport([("GET", path, 200, rows)])
    client = GitHubClient(GitHubConnection.parse({"id":"c","kind":"cloud"}), transport=wire)
    assert [s["sha"] for s in client.statuses(REPO, head)] == [head, "b"*40]


def test_git_child_token_downscoped_and_revoked(host_key):
    cfg = connection(host_key[0])
    access = token_response("isolated-read", 5000)[3]
    access.update(permissions={"contents":"read","metadata":"read"}, repositories=[{"id":6}])
    response = {"id":6, "full_name":REPO}
    wire = FakeTransport([token_response("studio-only", 5000), ("GET", "/repos/" + REPO, 200, response),
                          ("POST", "/app/installations/12/access_tokens", 201, access),
                          ("DELETE", "/installation/token", 204, None)])
    client = GitHubClient(cfg, transport=wire, clock=lambda: 100)
    with client.git_read_token(REPO) as read:
        assert read == "isolated-read"
    assert wire.requests[2][3] == {"repository_ids":[6], "permissions":{"contents":"read"}}
    assert wire.requests[3][2]["Authorization"] == "Bearer isolated-read"
    assert client._access() == "studio-only"


def test_unexpected_wide_git_scope_is_revoked_before_subprocess(host_key):
    cfg = connection(host_key[0])
    access = token_response("unexpected", 5000)[3]
    access.update(permissions={"contents": "write"}, repositories=[{"id": 6}])
    wire = FakeTransport([token_response("studio", 5000), ("GET", "/repos/" + REPO, 200, {"id": 6, "full_name": REPO}),
                          ("POST", "/app/installations/12/access_tokens", 201, access),
                          ("DELETE", "/installation/token", 204, None)])
    client = GitHubClient(cfg, transport=wire, clock=lambda:100)
    with pytest.raises(GitHubError, match="fetch_scope_unconfirmed"):
        with client.git_read_token(REPO, expected_repo_id=6):
            pytest.fail("Should not release this token")
    assert not wire.scripted


OWNER_DIGEST = "a" * 64
OWNER_LABELS = "/repos/" + REPO + "/labels"
OWNER_PATH = OWNER_LABELS + "/office%3Async-owner"


def owner_label(digest):
    return {"name": "office:sync-owner", "description": "Agent Office owner " + digest, "color": "6e7781"}


def test_owner_verification_reads_only_and_never_overwrites(host_key):
    wire = FakeTransport([("GET", OWNER_PATH, 404, {}), ("GET", OWNER_PATH, 200, owner_label(OWNER_DIGEST)),
                          ("GET", OWNER_PATH, 200, owner_label("b" * 64))])
    client = GitHubClient(connection(host_key[0]), transport=wire, clock=lambda: 0).for_repository(6)
    client._token, client._expires = "dummy", 3600
    with pytest.raises(GitHubError, match="^issue_owner_missing$"):
        client.ensure_issue_owner(REPO, OWNER_DIGEST)
    assert client.ensure_issue_owner(REPO, OWNER_DIGEST)
    with pytest.raises(GitHubError, match="^issue_owner_conflict$"):
        client.ensure_issue_owner(REPO, OWNER_DIGEST, claim=True)
    with pytest.raises(GitHubError, match="^invalid_issue_owner$"):
        client.ensure_issue_owner(REPO, "user text")
    assert [method for method, *_ in wire.requests] == ["GET"] * 3


@pytest.mark.parametrize("status,digest", [(201, OWNER_DIGEST), (422, OWNER_DIGEST), (503, OWNER_DIGEST),
                                            (422, "b" * 64), (503, None)])
def test_explicit_owner_claim_reads_authoritative_label_after_insert(host_key, status, digest):
    wire = FakeTransport([("GET", OWNER_PATH, 404, {}), ("POST", OWNER_LABELS, status, {}),
                          ("GET", OWNER_PATH, 200 if digest else 404, owner_label(digest) if digest else {})])
    client = GitHubClient(connection(host_key[0]), transport=wire, clock=lambda: 0).for_repository(6)
    client._token, client._expires = "dummy", 3600
    if digest == OWNER_DIGEST:
        assert client.ensure_issue_owner(REPO, OWNER_DIGEST, claim=True)
    else:
        code = "issue_owner_conflict" if digest else "issue_owner_unconfirmed"
        with pytest.raises(GitHubError, match="^" + code + "$"):
            client.ensure_issue_owner(REPO, OWNER_DIGEST, claim=True)
    assert wire.requests[1][3] == owner_label(OWNER_DIGEST)
    assert not wire.scripted


def test_two_independent_hosts_racing_can_only_claim_for_one_owner(host_key):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    class RepositoryLabels:
        def __init__(self):
            self.barrier, self.lock = Barrier(2), Lock()
            self.label = None
            self.methods = []

        def send(self, method, path, headers, body):
            with self.lock:
                self.methods.append(method)
                if method == "POST" and path == OWNER_LABELS:
                    if self.label:
                        return Response(422, {}, {})
                    self.label = body.copy()
                    return Response(201, {}, self.label)
                assert method == "GET" and path == OWNER_PATH
                if self.label:
                    return Response(200, {}, self.label)
            self.barrier.wait(timeout=10)
            return Response(404, {}, {})

    wire = RepositoryLabels()
    def enroll(digest):
        client = GitHubClient(connection(host_key[0]), transport=wire, clock=lambda: 0).for_repository(6)
        client._token, client._expires = "dummy", 3600
        try:
            return client.ensure_issue_owner(REPO, digest, claim=True)
        except GitHubError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enroll, [OWNER_DIGEST, "b"*64]))
    assert results.count(True) == 1 and results.count("issue_owner_conflict") == 1
    assert wire.methods.count("POST") == 2 and set(wire.methods) == {"GET", "POST"}


def test_repository_views_cannot_follow_a_recycled_slug_or_escalate(host_key):
    granted = grants(metadata="read", contents="write", pull_requests="write", issues="write", administration="write")
    limited = {"metadata":"read", "contents":"read", "pull_requests":"read", "issues":"write"}
    wire = FakeTransport([granted, scoped_response("repo-six", 5000, 6, **limited),
                          ("GET", "/repos/" + REPO, 200, {"id":6, "full_name":REPO}),
                          # GitHub now maps this old name to a different private repository.
                          ("POST", "/repos/" + REPO + "/issues/1/comments", 403, {}),
                          ("GET", "/repos/" + REPO, 200, {"id":7, "full_name":REPO})])
    global_client = GitHubClient(connection(host_key[0]), transport=wire, clock=lambda:0)
    repo = global_client.for_repository(6)
    assert repo is global_client.for_repository(6)
    assert repo is not global_client.for_repository(6, publish=True)
    assert repo.repository(REPO)["id"] == 6
    assert wire.requests[1][3] == {"repository_ids":[6], "permissions": limited}
    with pytest.raises(GitHubError, match="permission_denied"):
        repo.add_comment(REPO, 1, "verified status")
    assert wire.requests[-1][2]["Authorization"] == "Bearer repo-six"
    with pytest.raises(GitHubError, match="repository_identity_changed"):
        repo.repository(REPO)
    with pytest.raises(GitHubError, match="repository_scope_required"):
        global_client.add_comment(REPO, 1, "no")
    with pytest.raises(GitHubError, match="repository_scope_mismatch"):
        repo.for_repository(7)
    with pytest.raises(GitHubError, match="repository_scope_mismatch"):
        repo.for_repository(6, publish=True)
    with pytest.raises(GitHubError, match="publication_scope_required"):
        repo.create_draft(REPO, "ao/test", "main", "test", "body")
    assert not wire.scripted


@pytest.mark.parametrize("extras", [{"repositories":[{"id":6},{"id":7}]}, {"repositories":[]},
                                    {"repositories":[{"id":7}]}, {"permissions":{"issues":"write","administration":"write"}},
                                    {"permissions":None}])
def test_unknown_or_broader_grant_is_never_used_for_repo_work(host_key, extras):
    method, path, status, data = scoped_response("too-wide", 5000, 6, issues="write")
    wire = FakeTransport([grants(issues="write"), (method,path,status,{**data, **extras}),
                          ("DELETE","/installation/token",204,None)])
    client = GitHubClient(connection(host_key[0]), transport=wire, clock=lambda:0).for_repository(6)
    with pytest.raises(GitHubError, match="repository_scope_unconfirmed"):
        client.add_comment(REPO, 1, "no")
    assert not wire.scripted and client._token is None
    assert wire.requests[-1][2]["Authorization"] == "Bearer too-wide"


def test_draft_scope_requests_only_selected_repo_permissions(host_key):
    allowed = {"metadata":"read", "contents":"write", "pull_requests":"write", "checks":"read"}
    wire = FakeTransport([grants(**{**allowed,"checks":"write","actions":"write","administration":"read"}),
                          scoped_response("six-drafts", 5000, 6, **allowed),
                          ("POST", "/repos/" + REPO + "/pulls", 201, {"number":2})])
    client = GitHubClient(connection(host_key[0]), transport=wire, clock=lambda:0).for_repository(6, publish=True)
    assert client.create_draft(REPO, "ao/candidate", "main", "title", "body")["number"] == 2
    assert wire.requests[1][3] == {"repository_ids":[6], "permissions":allowed}
    assert wire.requests[2][2]["Authorization"] == "Bearer six-drafts"


def test_discovery_mints_verified_metadata_only(host_key):
    response = token_response("discovery-mock", 5000)
    wire = FakeTransport([response, ("GET", "/installation/repositories", 200, {"repositories":[]})])
    client = GitHubClient(connection(host_key[0]), transport=wire, clock=lambda:0)
    assert client.repositories() == []
    assert wire.requests[0][3] == {"permissions":{"metadata":"read"}}
    assert not wire.scripted

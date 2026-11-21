"""Exercise candidate publication against an isolated in-memory GitHub REST fixture."""
import base64
import hashlib
import json
from urllib.parse import urlsplit

import pytest

from agent_office.github_api import GitHubClient, GitHubConnection, GitHubError
from agent_office.github_connection import Response


REPO = "owner/candidate"
HEAD = "ao/issue-19-abc"
BASE_SHA = "1" * 40
BASE_TREE = "2" * 40


class GitObjects:
    def __init__(self):
        self.refs = {"main": BASE_SHA}
        self.commits = {BASE_SHA: {"sha": BASE_SHA, "tree": {"sha": BASE_TREE}, "parents": []}}
        self.trees = {}
        self.pr = None
        self.writes = []
        self.race = False

    def send(self, method, path, headers, body):
        path = urlsplit(path).path.removeprefix("/repos/" + REPO)
        if method != "GET":
            self.writes.append((method, path, body))
        if method == "POST" and path == "/git/blobs":
            data = base64.b64decode(body["content"])
            sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            return Response(201, {}, {"sha": sha})
        if method == "POST" and path == "/git/trees":
            sha = self.tree_id(body)
            self.trees[sha] = body
            return Response(201, {}, {"sha": sha})
        if method == "POST" and path == "/git/commits":
            sha = hashlib.sha1(json.dumps(body).encode()).hexdigest()
            self.commits[sha] = {"sha": sha, "parents": [{"sha": p} for p in body["parents"]], "tree": {"sha": body["tree"]}}
            return Response(201, {}, {"sha": sha})
        if method == "POST" and path == "/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.refs:
                return Response(422, {}, None)
            self.refs[name] = body["sha"]
            return Response(201, {}, {})
        if method == "GET" and path.startswith("/git/ref/heads/"):
            name = path.removeprefix("/git/ref/heads/")
            if name not in self.refs:
                return Response(404, {}, None)
            return Response(200, {}, {"ref": "refs/heads/"+name, "object": {"type": "commit", "sha": self.refs[name]}})
        if method == "PATCH" and path == "/git/refs/heads/" + HEAD:
            assert body["force"] is False
            if self.race or self.commits[body["sha"]]["parents"] != [{"sha": self.refs[HEAD]}]:
                return Response(422, {}, {"message": "non-fast-forward"})
            self.refs[HEAD] = body["sha"]
            return Response(200, {}, {})
        if method == "GET" and path.startswith("/git/commits/"):
            return Response(200, {}, self.commits[path.split("/")[-1]])
        if method == "GET" and path == "/pulls":
            return Response(200, {}, [self.pr] if self.pr else [])
        if method == "POST" and path == "/pulls":
            assert self.pr is None and body["draft"] and body["head"] == HEAD
            self.pr = {"number": 14, "state": "open", "draft": True, "user": {"type": "Bot"},
                       "performed_via_github_app": {"id": 12},
                       "base": {"ref": "main", "repo": {"full_name": REPO}},
                       "head": {"ref": HEAD, "repo": {"full_name": REPO}}}
            return Response(201, {}, self.pr)
        if method == "GET" and path == "/pulls/14":
            return Response(200, {}, {**self.pr, "head": {**self.pr["head"], "sha": self.refs[HEAD]}})
        pytest.fail("Unexpected route " + method + " " + path)

    @staticmethod
    def tree_id(body):
        return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()


@pytest.fixture
def app(tmp_path):
    http = GitObjects()
    cfg = GitHubConnection.parse({"id": "app", "kind": "cloud", "app_id": 12, "installation_id": 14,
                                  "private_key_file": str(tmp_path / "unused")})
    client = GitHubClient(cfg, transport=http, clock=lambda: 5)
    client._token, client._expires = "mock-installation", 1000
    return http, client


def publish(client, **kwargs):
    options = {"base_branch": "main", "base_sha": BASE_SHA, "head_branch": HEAD, "title": "test", "body": "fixture",
               "files": {"a.bin": ("100755", b"\x00\xff"), "old.py": None}}
    return client.publish_candidate(REPO, **{**options, **kwargs})


def test_retry_reuses_verified_branch_and_open_owned_draft(app):
    http, client = app
    result = publish(client)
    assert result["html_url"] == f"https://github.com/{REPO}/pull/14"
    tree = http.trees[result["tree_sha"]]
    assert tree["base_tree"] == BASE_TREE and tree["tree"][0]["mode"] == "100755" and tree["tree"][1]["sha"] is None
    assert publish(client, expected_tree=result["tree_sha"]) == result
    assert len([1 for _, path, _ in http.writes if path in ("/git/refs", "/pulls")]) == 2
    http.pr["performed_via_github_app"] = {"id": 56}
    with pytest.raises(GitHubError, match="draft_mismatch"):
        publish(client)


def test_wrong_expected_tree_never_creates_ref_or_commit_and_invalid_path_never_writes(app):
    http, client = app
    with pytest.raises(GitHubError, match="invalid_candidate_path"):
        publish(client, files={"../escape": b"x"})
    assert not http.writes
    with pytest.raises(GitHubError, match="candidate_tree_mismatch"):
        publish(client, expected_tree="a"*40)
    assert not any(path in ("/git/commits", "/git/refs", "/pulls") for _, path, _ in http.writes)


def test_followup_is_fast_forward_same_draft_and_idempotent(app):
    http, client = app
    first = publish(client)
    content = b"resolved"
    blob = hashlib.sha1(b"blob 8\0"+content).hexdigest()
    next_tree = http.tree_id({"base_tree": first["tree_sha"],
                              "tree": [{"path": "a.txt", "mode": "100644", "type": "blob", "sha": blob}]})
    options = dict(base_branch="main", head_branch=HEAD, expected_head=first["head_sha"], pr_number=14,
                   files={"a.txt": content}, expected_tree=next_tree, title="follow-up")
    http.race = True
    with pytest.raises(GitHubError, match="validation_failed"):
        client.advance_candidate(REPO, **options)
    assert http.refs[HEAD] == first["head_sha"]
    http.race = False
    followup = client.advance_candidate(REPO, **options)
    assert followup["number"] == 14 and followup["head_sha"] != first["head_sha"]
    writes = len(http.writes)
    assert client.advance_candidate(REPO, **options) == followup
    assert len(http.writes) == writes and http.refs["main"] == BASE_SHA
    http.pr["draft"] = False
    with pytest.raises(GitHubError, match="draft_mismatch"):
        client.advance_candidate(REPO, **options)
    assert len(http.writes) == writes

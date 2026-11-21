"""A verified immutable Office tree must be the tree routed to and from a GitHub PR."""
from types import SimpleNamespace
from uuid import uuid4
import subprocess

import pytest

from agent_office import goal_verification
from agent_office.github_office import intake_details, remote_name
from agent_office.github_connection import GitHubError
from agent_office.github_owner import verify_owner
from agent_office.github_studio import WorkspaceGitHub, check_reply
from agent_office.studio_ssh import RemoteUnavailable, is_peer_route
from agent_office.github_sync import IssueSync
from agent_office.goal_candidates import capture
from agent_office.store import TaskStore, encode, now
from agent_office.workspace import create_worktree
from helpers.github_owner import OwnerClaims
from test_studio_http import project, studio, request
from test_workspace_rooms import engine_snapshot


class Client(OwnerClaims):
    web_url = "https://git.example.org"
    app_id = 19
    identity = "test-installation"
    config = SimpleNamespace(owner_journal=None)

    def __init__(self, sha):
        super().__init__()
        self.sha = sha
        self.head = "e"*40

    def repository(self, slug):
        assert slug == "team/repo"
        return {"id":35,"full_name":slug}

    def issue(self, slug, number):
        return dict(self.issue_data, state="open", labels=[{"name":"office:ready"}])

    def timeline(self, slug, number):
        return [{"event":"labeled","actor":{"id":2,"login":"operator","type":"User"},"label":{"name":"office:ready"}}]

    def permission(self, *args):
        return "write"

    def is_app_actor(self, event):
        return False

    def for_repository(self, repo_id, *, publish=False):
        assert type(repo_id) is int and repo_id > 0
        return self

    def branch_sha(self, slug, branch):
        return self.sha if branch == "main" else self.head

    def commit_tree(self, slug, sha):
        return self.tree

    def diagnose(self):
        return {"ok":True,"code":"ok"}

    def publish_candidate(self, slug, **options):
        self.options = options
        self.tree = options["expected_tree"]
        return {"number":7,"head_sha":self.head,"html_url":self.web_url + "/team/repo/pull/7"}

    def pull(self, slug, number):
        return {"state":"open","number":number,"base":{"ref":"main","repo":{"id":35}},
                "head":{"sha":self.head,"ref":self.options["head_branch"],"repo":{"id":35}}}


def test_binding_intake_and_ci_repair_pin_immutable_tree_and_keep_same_goal(tmp_path, monkeypatch):
    repo = project(tmp_path)[0].parent
    def git(*args):
        return subprocess.check_output(["git","-C",str(repo),*args], text=True).strip()
    git("branch","-M","main")
    git("remote","add","origin","git@git.example.org:team/repo.git")
    client = Client(git("rev-parse","HEAD"))
    registry = SimpleNamespace(client=lambda _:client, connections=lambda:[])
    monkeypatch.setenv("AGENT_OFFICE_GITHUB_OWNER_DB",str(tmp_path/"owners.sqlite3"))
    monkeypatch.setattr(IssueSync, "start", lambda self:None)
    db_path = tmp_path / "studio.sqlite3"
    with studio(db_path) as server:
        server.github.close()
        server.github = WorkspaceGitHub(server, registry)
        office = server.github.sync.office
        monkeypatch.setattr(server.runner,"_launch",lambda *args:None)
        monkeypatch.setattr(server.engines,"snapshot",lambda:engine_snapshot("codex","antigravity"))
        _, created = request(server,"/api/workspaces",{"name":"GitHub fixture","paths":[str(repo)]})
        ws = created["workspace"]["id"]
        path = "/api/rooms/" + ws + "/github"
        binding = {"connection_id":"corp","repository_id":35,"folder_index":0,"branch":"main"}
        status, test = request(server,path + "/test",binding)
        assert status == 200 and test["diagnostic"]["base_sha"] == client.sha
        _, bad = request(server,path + "/test",dict(binding,repository_id=36))
        assert not bad["diagnostic"]["ok"]
        status, data = request(server,path,dict(binding,allowed_actors=[2],enabled=True,draft_pr=True))
        assert status == 200, data
        cfg = data["config"]
        check_reply(200, data, "POST", path.removeprefix("/api"))
        with pytest.raises(RemoteUnavailable):
            check_reply(200, dict(data,workspace_id=uuid4().hex), "GET", path.removeprefix("/api"))
        assert is_peer_route("GET", path.removeprefix("/api") + "/issues/123/page/12")
        assert not is_peer_route("POST", path.removeprefix("/api") + "/issues/123/page/12")
        first = uuid4().hex
        issue = {"id":123,"number":3,"title":"설정 복원","body":"## 완료 기준\n- [ ] 설정을 유지한다", "goal_id":first}
        client.issue_data = issue
        receipt = office.start(ws,first,issue,cfg)
        assert office.start(ws,first,issue,cfg) == receipt
        with pytest.raises(ValueError):
            office.publish(ws,first,cfg,client,issue)
        tree = create_worktree(repo, revision=client.sha)
        (tree.path / "web/value.txt").write_text("verified\n", encoding="utf8")
        files, patch, digest = capture(tree.path,client.sha,["."])
        candidate = dict(attempt=1,digest=digest,score=10,files=files,eligible=True,status="passed")
        with TaskStore(db_path,existing=True) as store, store._write():
            db = store.connection
            db.execute("UPDATE runs SET state='READY_FOR_HUMAN_REVIEW',outcome='APPROVED',ended_at=? WHERE run_id=?", (now(),first))
            goal_verification.init_schema(db)
            db.execute("INSERT INTO studio_goal_candidates VALUES(?,?,?,?)",(first,1,encode(candidate),patch))
            db.execute("INSERT INTO studio_goal_reviews VALUES(?,?,?,?,?,?,?)",(first,1,"review",digest,"approved","approved",now()))
        (tree.path / "web/value.txt").write_text("mutated after review\n", encoding="utf8")
        pr = office.publish(ws,first,cfg,client,issue)
        # Git may normalize LF when making the original checkout; only captured bytes are used.
        assert client.options["files"]["web/value.txt"] == ("100644",b"verified\n")
        issue.update(pr_number=7,pr_head=pr["head"],candidate_sha=client.head)
        second = uuid4().hex
        retry = office.rework(ws,first,second,"CI 회귀 원인 확인",cfg,issue,client.head)
        assert retry["goal_id"] == first
        assert git("rev-parse",retry["base_commit"]+"^{tree}") == client.tree
        assert git("rev-parse","HEAD") == client.sha
        _, report = request(server, "/api/runs/"+second+"/goal")
        assert report["goal"]["id"] == first and report["goal"]["run_id"] == second
        assert [r["id"] for r in report["goal"]["external"]["linked_runs"]] == [first,second]
        client.head = "f"*40
        with pytest.raises(ValueError):
            office.rework(ws,first,uuid4().hex,"뒤늦은 CI",cfg,issue,"e"*40)


def test_repository_owner_is_persistent_and_explicit_pin_rejects_another_journal():
    client = Client("3"*40)
    with pytest.raises(GitHubError, match="issue_owner_missing"):
        verify_owner("a"*32,client,4,"example/repo")
    verify_owner("a"*32,client,4,"example/repo",claim=True)
    verify_owner("a"*32,client,4,"example/repo")
    with pytest.raises(GitHubError, match="issue_owner_conflict"):
        verify_owner("b"*32,client,4,"example/repo",claim=True)
    assert len(client.owner_markers) == 1
    client.config = SimpleNamespace(owner_journal="a"*32)
    with pytest.raises(GitHubError, match="owner_journal_mismatch"):
        verify_owner("b"*32,client,7,"example/other",claim=True)


def test_issue_material_and_origin_are_scoped():
    assert remote_name("git@host.example:a/b.git","https://host.example") == "a/b"
    for origin in ("https://other.example/a/b","https://token@host.example/a/b","file:///local/repo",
                   "https://host.example/a/b?token=x"):
        with pytest.raises(ValueError):
            remote_name(origin,"https://host.example")
    details = intake_details({"title":"issue", "body":"- [ ] cold start\nhttps://example.org/doc?signature=private"})
    assert details["criteria"] == ["cold start"] and details["links"] == ["https://example.org/doc"]

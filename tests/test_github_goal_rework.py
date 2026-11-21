"""Real SQLite intake and Git candidate reconstruction against a local provider double."""
from hashlib import sha256
from types import SimpleNamespace

from agent_office import goal_verification, overnight_goals
from agent_office.github_office import GitHubOffice
from agent_office.github_sync import IssueSync
from agent_office.release_store import run_git
from agent_office.store import TaskStore, encode, now
from helpers.github_owner import OwnerClaims
from test_github_sync import Remote, check
from test_studio_http import project, request, studio
from test_workspace_rooms import engine_snapshot


class CandidateProvider(OwnerClaims, Remote):
    def __init__(self, repo):
        super().__init__()
        self.config = SimpleNamespace(owner_journal=None)
        self.repo = repo
        self.base = run_git(repo, "rev-parse", "HEAD").decode().strip()
        self.refs = {"main": self.base}
        self.publications = []

    def branch_sha(self, slug, branch):
        return self.refs[branch]

    def commit_tree(self, slug, sha):
        return run_git(self.repo, "rev-parse", sha + "^{tree}").decode().strip()

    def _publish(self, head, tree, parent, files):
        assert self.refs.get(head, self.base) == parent
        sha = run_git(self.repo, "commit-tree", tree, "-p", parent, data=b"Provider fixture\n").decode().strip()
        for name, (_, content) in files.items():
            assert run_git(self.repo, "show", sha + ":" + name) == content
        self.refs[head] = sha
        self.pull_data["head"].update(ref=head, sha=sha)
        self.publications.append((sha, files))
        return {"number": 5, "head_sha": sha, "html_url": self.web_url + "/owner/repo/pull/5"}

    def publish_candidate(self, slug, *, base_branch, base_sha, head_branch, files, expected_tree, **kwargs):
        assert base_branch == "main" and base_sha == self.base and not self.publications
        return self._publish(head_branch, expected_tree, base_sha, files)

    def advance_candidate(self, slug, *, base_branch, head_branch, expected_head, pr_number, files, expected_tree, title):
        assert pr_number == 5 and self.publications[-1][0] == expected_head
        return self._publish(head_branch, expected_tree, expected_head, files)

    def checks(self, repo, sha):
        return [record for record in self.results if record["head_sha"] == sha]


def record_reviewed_candidate(db, run_id, name):
    # The worker's review and test receipt are seeded. Git reconstruction and
    # coordinator selection, run lineage, publication and reconciliation are real.
    patch = (f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n"
             "@@ -0,0 +1 @@\n+accepted candidate\n")
    with TaskStore(db, existing=True) as store, store._write():
        conn = store.connection
        base = conn.execute("SELECT base_commit FROM studio_goals WHERE id=?", (run_id,)).fetchone()[0]
        digest = sha256((base + "\0" + patch).encode()).hexdigest()
        record = dict(attempt=1, digest=digest, eligible=True, score=1, status="passed", files=[name])
        goal_verification.init_schema(conn)
        conn.execute("INSERT INTO studio_goal_candidates VALUES(?,?,?,?)", (run_id, 1, encode(record), patch))
        conn.execute("INSERT INTO studio_goal_reviews VALUES(?,?,?,?,?,?,?)",
                     (run_id, 1, "review", digest, "approved", "local fixture", now()))
        conn.execute("UPDATE runs SET state='READY_FOR_HUMAN_REVIEW',outcome='APPROVED',ended_at=started_at "
                     "WHERE run_id=?", (run_id,))


def test_external_rework_starts_at_verified_tree_then_advances_same_pr(tmp_path, monkeypatch):
    repo = project(tmp_path)[0].parent
    run_git(repo, "remote", "add", "origin", "https://provider.test/owner/repo.git")
    provider = CandidateProvider(repo)
    database = tmp_path / "office.db"
    monkeypatch.setenv("AGENT_OFFICE_GITHUB_OWNER_DB", str(tmp_path / "owners.db"))
    with studio(database) as server:
        monkeypatch.setattr(server.runner, "_launch", lambda *args: None)
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("codex", "antigravity"))
        code, created = request(server, "/api/workspaces", {"name": "settings", "paths": [str(repo)]})
        assert code == 201
        ws = created["workspace"]["id"]
        sync = IssueSync(database, provider, GitHubOffice(server, provider))
        sync.configure(ws, dict(connection_id="test", repository_id=99, repository="owner/repo", branch="main",
                                allowed_actors=[11], draft_pr=True, enabled=True, ci_retries=1, ci_checks=["unit"]))
        def poll():
            sync.run_once(ws)
            state = sync.state(ws)
            assert state["status"]["last_error"] is None, state
            return state["issues"][0]

        # On cold startup discovery can run before the engine probe finishes.
        engines = engine_snapshot("codex", "antigravity")
        for engine in engines["engines"]:
            engine["probe_status"] = "pending"
        monkeypatch.setattr(server.engines, "snapshot", lambda: engines)
        sync.run_once(ws)
        waiting = sync.state(ws)["issues"][0]
        assert waiting["status"] == "starting" and waiting["run_id"] is None
        assert waiting["goal_id"] and waiting["last_error"]
        sync = IssueSync(database, provider, GitHubOffice(server, provider))
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("codex", "antigravity"))
        first = poll()
        assert first["run_id"] == waiting["goal_id"]
        record_reviewed_candidate(database, first["run_id"], "first.txt")
        published = poll()
        assert published["pr_number"] == 5
        provider.results = [check("failure", head_sha=published["candidate_sha"])]
        second = poll()
        assert second["run_id"] != first["run_id"]
        goal = overnight_goals.report(database, second["run_id"])
        assert goal["external"]["previous_run"] == first["run_id"]
        assert goal["external"]["goal_id"] == first["goal_id"]
        assert goal["external"]["head_sha"] == published["candidate_sha"]
        assert provider.commit_tree("owner/repo", goal["base_commit"]) == provider.commit_tree(
            "owner/repo", published["candidate_sha"])
        record_reviewed_candidate(database, second["run_id"], "second.txt")
        finished = poll()
        assert finished["pr_number"] == 5 and finished["candidate_sha"] != published["candidate_sha"]
        assert run_git(repo, "show", finished["candidate_sha"] + ":first.txt") == b"accepted candidate\n"
        assert run_git(repo, "show", finished["candidate_sha"] + ":second.txt") == b"accepted candidate\n"
        poll()
        assert len(provider.publications) == 2
        assert not (repo / "first.txt").exists() and run_git(repo, "rev-parse", "HEAD").decode().strip() == provider.base

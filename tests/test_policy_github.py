from copy import deepcopy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent_office import policy_github as policy
from agent_office.release_store import run_git


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    directory = tmp_path_factory.mktemp("operator") / "objects.git"
    directory.mkdir()

    def git(*args):
        return run_git(directory, *args).decode().strip()

    git("init", "--bare", "--template=")
    git("config", "remote.origin.url", f"https://github.com/{policy.REPOSITORY}.git")
    blob = run_git(directory, "hash-object", "-w", "--stdin", data=b"workflow").decode().strip()

    def tree(line):
        return run_git(directory, "mktree", data=line.encode()).decode().strip()

    def github_tree(test_command):
        source = f"jobs:\n  verify:\n    steps:\n      - uses: actions/checkout@v4\n      - run: {test_command}\n"
        wf = run_git(directory, "hash-object", "-w", "--stdin", data=source.encode()).decode().strip()
        workflows = tree("".join(f"100644 blob {wf}\t{name}\n" for name in ("ci.yml", "desktop.yml", "rust-comparison.yml")))
        return tree(f"040000 tree {workflows}\tworkflows\n")

    base_tree = tree(f"040000 tree {github_tree('python -m pytest tests/unit')}\t.github\n")
    head_tree = tree(f"040000 tree {github_tree('python -m pytest tests/')}\t.github\n100644 blob {blob}\tfile.txt\n")
    base = git("commit-tree", base_tree, "-m", "operator stable")
    head = git("commit-tree", head_tree, "-p", base, "-m", "pull request")
    merge = git("commit-tree", head_tree, "-p", base, "-p", head, "-m", "merge on main")
    tip = git("commit-tree", head_tree, "-p", merge, "-m", "another main change arrived before poll")
    git("update-ref", "refs/remotes/origin/main", tip)
    return SimpleNamespace(base=base, head=head, tree=head_tree, merge=merge, tip=tip, directory=directory)


@pytest.fixture
def github(source, monkeypatch):
    repo = {"full_name": policy.REPOSITORY}
    branch = "feature/from-same-repo"
    synthetic = "a" * 40
    payloads = {}
    runs = []
    run_query = f"actions/runs?head_sha={source.head}&event=pull_request&per_page=10"
    started = {"status": "completed", "conclusion": "success"}
    for index, (path, (workflow, names)) in enumerate(policy.WORKFLOWS.items(), start=1):
        reference = {"sha": synthetic, "ref": "refs/pull/7/merge",
                     "path": f"{policy.REPOSITORY}/{policy.RUST}@{synthetic}"}
        runs.append({**started, "id": index, "check_suite_id": index * 10, "run_attempt": 2,
                     "head_branch": branch, "head_sha": source.head, "repository": repo, "head_repository": repo,
                     "event": "pull_request", "pull_requests": [], "workflow_id": workflow, "path": path,
                     "referenced_workflows": [] if path == policy.CI else [reference]})
        payloads[f"check-suites/{index * 10}"] = {
            "id": index * 10, "repository": repo, "head_sha": source.head,
            "app": {"id": 15368, "slug": "github-actions"}}
        jobs = [{**started, "id": index * 100 + j, "name": name, "run_id": index, "run_attempt": 2,
                 "head_sha": source.head, "labels": [system for system in policy.SYSTEMS if system in name],
                 "runner_group_id": 0, "runner_group_name": "GitHub Actions",
                 "steps": [{**started, "number": 1, "name": "Set up job"},
                           {**started, "number": 2, "name": "Run actions/checkout@v4"}]}
                for j, name in enumerate(sorted(names))]
        payloads[f"actions/runs/{index}/attempts/2/jobs?per_page=10"] = {"total_count": len(jobs), "jobs": jobs}
    payloads[run_query] = {"total_count": len(runs), "workflow_runs": runs}
    payloads["git/ref/heads/main"] = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": source.tip}}
    payloads[f"commits/{source.merge}/pulls?per_page=2"] = [{"number": 7}]
    payloads["pulls/7"] = {"number": 7, "state": "closed", "merged": True, "merge_commit_sha": source.merge,
                           "base": {"ref": "main", "repo": repo, "sha": source.base},
                           "head": {"ref": branch, "repo": repo, "sha": source.head}}
    commit = {"sha": synthetic, "tree": {"sha": source.tree},
              "parents": [{"sha": source.base}, {"sha": source.head}]}
    payloads[f"git/commits/{synthetic}"] = commit
    log_path = "actions/jobs/100/logs"
    log_data = (
        f"2025-12-31T23:59:59Z Download action repository 'actions/checkout@v4' (SHA:{'1' * 40})\r\n"
        f"2026-01-01T00:00:00Z ##[group]Run actions/checkout@v4\r\n"
        f"2026-01-01T00:00:01Z [command]/usr/bin/git fetch --depth=1 origin +{synthetic}:refs/remotes/pull/7/merge\r\n"
        f"2026-01-01T00:00:02Z [command]/usr/bin/git log -1 --format=%H\r\n"
        f"2026-01-01T00:00:03Z {synthetic}\r\n"
        "2026-01-01T00:00:04Z ##[group]Run actions/setup-python@v5\r\n").encode()
    log_paths = {f"actions/jobs/{index * 100 + j}/logs" for index in (1, 2) for j in range(4)}
    for path in log_paths:
        payloads[path] = log_data
    calls = []

    def request(gh, path, *, log=False):
        assert log == (path in log_paths)
        calls.append(path)
        return deepcopy(payloads[path])

    monkeypatch.setattr(policy, "_request", request)
    return SimpleNamespace(payloads=payloads, request=request, runs=runs, query=run_query, calls=calls,
                           log_path=log_path, commit=commit, pull=payloads["pulls/7"],
                           jobs=payloads["actions/runs/1/attempts/2/jobs?per_page=10"]["jobs"])


def attest(source, **args):
    params = dict(repository=policy.REPOSITORY, revision=source.merge, base=source.base, git_dir=source.directory)
    return policy.attest(**(params | args))


def test_complete_latest_attempt_is_bound_to_the_pr_and_tree(source, github):
    proof = attest(source)
    assert (proof.pr, proof.head, proof.merge, proof.tree) == (7, source.head, source.merge, source.tree)
    assert proof.run_ids == (1, 2) and proof.attempts == (2, 2) and len(proof.check_names) == 8
    assert proof.tested_merge == "a" * 40
    assert github.calls.count(github.query) == github.calls.count("git/ref/heads/main") == 2
    assert len({path for path in github.calls if path.endswith("/logs")}) == 8
    with pytest.raises(FrozenInstanceError):
        proof.pr = 8


def test_workflow_changes_after_checkout_are_eligible(source, github):
    before, after = [run_git(source.directory, "rev-parse", f"{sha}:.github/workflows")
                     for sha in (source.base, source.merge)]
    assert before != after
    assert attest(source).merge == source.merge


@pytest.mark.parametrize("job_id", [100, 101, 102, 103, 200, 201, 202, 203])
def test_every_job_must_have_checked_out_the_tested_merge(source, github, job_id):
    path = f"actions/jobs/{job_id}/logs"
    github.payloads[path] = github.payloads[path].replace(b"a" * 40, b"c" * 40)
    with pytest.raises(ValueError, match="different PR merge ref"):
        attest(source)


@pytest.mark.parametrize("first_step", [
    "name: Run actions/checkout@v4\n        run: echo checkout",
    "uses: untrusted/checkout@v4", "uses: actions/checkout@v4\n        with: {ref: other}",
])
def test_a_display_name_cannot_impersonate_a_default_checkout(first_step):
    with pytest.raises(ValueError, match="default official checkout"):
        policy._checkout_workflow("jobs:\n  test:\n    steps:\n      - " + first_step)


@pytest.mark.parametrize(("field", "value"), [
    ("path", ".github/workflows/copied.yml"), ("workflow_id", 1), ("event", "workflow_dispatch"),
    ("event", "merge_group"), ("event", "pull_request_target"), ("head_sha", "c" * 40),
    ("head_branch", "other"), ("repository", {"full_name": "wrong/project"}),
    ("head_repository", {"full_name": "fork/project"}), ("pull_requests", [{"number": 9}]),
    ("status", "in_progress"), ("conclusion", "failure")])
def test_wrong_workflow_provenance_or_status(source, github, field, value):
    github.runs[0][field] = value
    with pytest.raises(ValueError):
        attest(source)


@pytest.mark.parametrize(("field", "value"), [
    ("name", "same-named external check"), ("name", "windows-latest / Python 3.13"),
    ("id", 101), ("head_sha", "e" * 40), ("run_id", 1000), ("run_attempt", 1), ("conclusion", "skipped"),
    ("labels", ["self-hosted"]), ("runner_group_id", 1), ("steps", [])])
def test_reject_missing_duplicate_wrong_head_and_previous_attempt_jobs(source, github, field, value):
    github.jobs[0][field] = value
    with pytest.raises(ValueError):
        attest(source)


@pytest.mark.parametrize("scenario", ["wrong app", "wrong suite head", "open PR", "other merge", "other PR",
                                       "wrong base branch", "wrong base SHA", "wrong source repo", "wrong source head", "wrong tree",
                                       "intervening main", "other rust workflow", "other merge ref", "older CI ref",
                                       "later forged log", "extra run", "missing job", "duplicate workflow"])
def test_refuse_ambiguous_or_unrelated_evidence(source, github, scenario):
    suite = github.payloads["check-suites/10"]
    ref, = github.runs[1]["referenced_workflows"]
    payloads = github.payloads
    match scenario:
        case "wrong app":
            suite["app"]["id"] = 9
        case "wrong suite head":
            suite["head_sha"] = "c" * 40
        case "open PR":
            github.pull["merged"] = False
        case "other merge":
            github.pull["merge_commit_sha"] = source.head
        case "other PR":
            payloads[f"commits/{source.merge}/pulls?per_page=2"].append({"number": 8})
        case "wrong base branch":
            github.pull["base"]["ref"] = "release"
        case "wrong base SHA":
            github.pull["base"]["sha"] = source.head
        case "wrong source repo":
            github.pull["head"]["repo"] = {"full_name": "fork/project"}
        case "wrong source head":
            github.pull["head"]["sha"] = source.base
        case "wrong tree":
            github.commit["tree"]["sha"] = source.head
        case "intervening main":
            github.commit["parents"][0]["sha"] = source.head
        case "other rust workflow":
            ref["path"] = ref["path"].replace("rust-comparison", "almost-rust")
        case "other merge ref":
            ref["ref"] = "refs/pull/9/merge"
        case "older CI ref":
            payloads[github.log_path] = payloads[github.log_path].replace(b"a" * 40, b"b" * 40)
        case "later forged log":
            log = payloads[github.log_path]
            payloads[github.log_path] = log.replace(b"/pull/7/", b"/pull/8/") + log
        case "extra run":
            payloads[github.query]["total_count"] += 1
        case "missing job":
            github.jobs.pop()
        case "duplicate workflow":
            github.runs[0].update(github.runs[1])
    with pytest.raises(ValueError):
        attest(source)


@pytest.mark.parametrize("scenario", ["main advanced", "newer rerun", "newer run failed"])
def test_recheck_main_and_latest_runs_after_reading_jobs(source, github, monkeypatch, scenario):
    def race(gh, path, *, log=False):
        value = github.request(gh, path, log=log)
        if path == github.query and github.calls.count(path) == 2:
            if scenario == "newer rerun":
                value["workflow_runs"][0]["run_attempt"] += 1
            if scenario == "newer run failed":
                value["workflow_runs"][0]["conclusion"] = "failure"
        if scenario == "main advanced" and path == "git/ref/heads/main" and github.calls.count(path) == 2:
            value["object"]["sha"] = source.head
        return value

    monkeypatch.setattr(policy, "_request", race)
    with pytest.raises(ValueError):
        attest(source)


def test_only_the_operator_origin_and_direct_predecessor_are_eligible(source, github, tmp_path):
    with pytest.raises(ValueError, match="direct main"):
        attest(source, base=source.head)
    with pytest.raises(ValueError, match="unsupported repository"):
        attest(source, repository="other/repo")
    run_git(tmp_path, "init", "--bare", "--template=")
    run_git(tmp_path, "config", "remote.origin.url", "https://example.invalid/other.git")
    with pytest.raises(ValueError, match="origin"):
        attest(source, git_dir=tmp_path)
    assert github.calls == []


def test_side_merge_is_not_a_main_first_parent_release(source, github, tmp_path):
    # Use a non-bare private operator clone. Its dirty worktree is never inspected.
    run_git(tmp_path, "init", "--template=")
    run_git(tmp_path, "fetch", "--no-tags", source.directory.as_uri(), source.tip)
    run_git(tmp_path, "remote", "add", "origin", f"https://github.com/{policy.REPOSITORY}.git")
    side = run_git(tmp_path, "commit-tree", source.tree, "-p", source.base, "-p", source.head,
                   "-m", "side merge").decode().strip()
    tip = run_git(tmp_path, "commit-tree", source.tree, "-p", source.merge, "-p", side,
                  "-m", "side later integrated").decode().strip()
    run_git(tmp_path, "update-ref", "refs/remotes/origin/main", tip)
    github.payloads["git/ref/heads/main"]["object"]["sha"] = tip
    assert attest(source, git_dir=tmp_path).merge == source.merge
    with pytest.raises(ValueError, match="first-parent"):
        attest(source, revision=side, git_dir=tmp_path / ".git")

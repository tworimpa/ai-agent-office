"""Read-only release evidence from the operator's Git object store and GitHub."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess

import yaml

from .release_validation import _execute, _tool_env


REPOSITORY = "tworimpa/ai-agent-office"
DEFAULT_GH = Path(r"C:\Program Files\GitHub CLI\gh.exe" if os.name == "nt" else "/usr/bin/gh")
CI = ".github/workflows/ci.yml"
DESKTOP = ".github/workflows/desktop.yml"
# The Desktop workflow calls this one reusable workflow. GitHub records that call in the
# run's `referenced_workflows` with the commit the pull request run tested
# (refs/pull/N/merge); no other field of a run names that commit.
TESTED_MERGE = ".github/workflows/tested-merge.yml"
SYSTEMS = ("ubuntu-latest", "windows-latest")
WORKFLOWS = {
    CI: (408869156, {f"{system} / Python {version}" for system in SYSTEMS for version in ("3.11", "3.13")}),
    DESKTOP: (424727684, {f"{system} desktop installer" for system in SYSTEMS}
              | {"tested-merge / ubuntu-latest / Tested merge"}),
}
SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Attestation:
    pr: int
    head: str
    merge: str
    tree: str
    run_ids: tuple[int, ...]
    check_names: tuple[str, ...]
    attempts: tuple[int, ...]
    tested_merge: str


def _require(condition, reason):
    if not condition:
        raise ValueError(f"Release attestation refused: {reason}")


def _sha(value):
    _require(isinstance(value, str) and SHA.fullmatch(value), "invalid commit or tree ID")
    return value


def _id(value):
    _require(type(value) is int and value > 0, "invalid GitHub ID")
    return value


def _request(gh, path, *, log=False):
    # Only gh resolves its own authentication. Do not inherit endpoint, proxy,
    # debug, pager or CLI configuration overrides from a worker process.
    env = _tool_env()
    env.update({k: os.environ[k] for k in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                                           "XDG_CONFIG_HOME", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ})
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", GH_NO_EXTENSION_UPDATE_NOTIFIER="1")
    status, raw = _execute(
        [str(gh), "api", "--hostname", "github.com", "--method", "GET", f"repos/{REPOSITORY}/{path}"],
        env=env, timeout=25, limit=(8 << 20 if log else 1 << 20), stderr=subprocess.DEVNULL)
    _require(status == 0, "GitHub evidence unavailable")
    return raw if log else json.loads(raw)


def _git(git_dir, *args, limit=4096):
    # git_dir is operator-owned. A worker's repository configuration is never loaded.
    status, data = _execute(
        ["git", "--no-replace-objects", f"--git-dir={git_dir}", *args],
        env={**_tool_env(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
             "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
        limit=limit, timeout=10, stderr=subprocess.DEVNULL)
    _require(status == 0, "trusted Git object or configuration unavailable")
    return data.decode("utf-8").strip()


def _success(record):
    _require(record["status"] == "completed" and record["conclusion"] == "success",
             "required GitHub Actions run or job did not succeed")


def _runs(get, head, branch, pr):
    response = get(f"actions/runs?head_sha={head}&event=pull_request&per_page=10")
    runs = response["workflow_runs"]
    _require(response["total_count"] == len(runs) == len(WORKFLOWS), "missing or ambiguous workflow runs")
    result = {}
    for run in runs:
        path = run["path"]
        _require(path in WORKFLOWS and path not in result and run["workflow_id"] == WORKFLOWS[path][0],
                 "unexpected or duplicate workflow identity")
        _require(run["repository"]["full_name"] == run["head_repository"]["full_name"] == REPOSITORY
                 and run["event"] == "pull_request" and run["head_sha"] == head
                 and run["head_branch"] == branch, "workflow does not belong to the exact PR head")
        # GitHub returns an empty association after a branch has been deleted.
        # Both workflows must independently prove their numbered refs/pull/N/merge below.
        _require(isinstance(run["pull_requests"], list) and all(
            item["number"] == pr for item in run["pull_requests"]), "workflow associated with another PR")
        _success(run)
        result[path] = (_id(run["id"]), _id(run["run_attempt"]), _id(run["check_suite_id"]),
                        run["referenced_workflows"])
    _require(len({row[0] for row in result.values()}) == len(result)
             and len({row[2] for row in result.values()}) == len(result), "duplicate Actions run or suite")
    return result


def _jobs(get, path, run, head):
    run_id, attempt, suite_id, _ = run
    suite = get(f"check-suites/{suite_id}")
    _require(suite["id"] == suite_id and suite["app"]["id"] == 15368 and suite["app"]["slug"] == "github-actions"
             and suite["head_sha"] == head and suite["repository"]["full_name"] == REPOSITORY,
             "check suite was not produced by the expected GitHub Actions app")
    response = get(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=10")
    jobs = {}
    for job in response["jobs"]:
        name = job["name"]
        _require(name not in jobs and name in WORKFLOWS[path][1] and job["run_id"] == run_id
                 and job["run_attempt"] == attempt and job["head_sha"] == head,
                 "unexpected, stale or duplicate job")
        _success(job)
        system, = [os_label for os_label in SYSTEMS if os_label in name]
        _require(job["runner_group_id"] == 0 and job["runner_group_name"] == "GitHub Actions"
                 and job["labels"] == [system], "required job did not use the expected hosted runner")
        setup, checkout = job["steps"][:2]
        _require((setup["number"], setup["name"]) == (1, "Set up job")
                 and (checkout["number"], checkout["name"]) == (2, "Run actions/checkout@v4"),
                 "job did not start with the expected checkout action")
        _success(setup)
        _success(checkout)
        jobs[name] = _id(job["id"])
    _require(response["total_count"] == len(jobs) and jobs.keys() == WORKFLOWS[path][1]
             and len(set(jobs.values())) == len(jobs), "incomplete or ambiguous job matrix")
    return jobs


def _checkout_workflow(source):
    # A user-authored shell step can choose the same display name as an action.
    # Verify the actual first executable step against the workflow Git object.
    try:
        depth = 0
        for event in yaml.parse(source, Loader=yaml.BaseLoader):
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            _require(depth < 64 and not isinstance(event, yaml.AliasEvent) and not getattr(event, "tag", None),
                     "unsupported workflow nesting, alias or tag")
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        _require(all(key.islower() and key != "<<" for key in workflow), "ambiguous workflow fields")
        _require(isinstance(workflow["jobs"], dict) and workflow["jobs"], "missing workflow jobs")
        for job in workflow["jobs"].values():
            _require(all(key.islower() and key != "<<" for key in job), "ambiguous job fields")
            if "uses" in job:
                _require(job["uses"] == "./" + TESTED_MERGE, "unknown reusable workflow")
                continue
            _require(job["steps"][0] == {"uses": "actions/checkout@v4"},
                     "the first workflow step must be the default official checkout")
            # A container or inherited process override could change the action or its git executable.
            # Other job settings and the subsequent build and test steps may evolve normally.
            _require(not {"container", "services"} & job.keys(), "containerized checkout cannot be attested")
            for env in (workflow.get("env", {}), job.get("env", {})):
                _require(isinstance(env, dict) and set(env) <= {"CSC_IDENTITY_AUTO_DISCOVERY"},
                         "unknown workflow environment during checkout")
    except (yaml.YAMLError, KeyError, AttributeError, TypeError, IndexError, RecursionError) as exc:
        raise ValueError("Malformed checkout workflow definition") from exc


def _checkout_evidence(log, pr, synthetic):
    # The hosted runner starts a new group for each executable step. Check only
    # the first such group, before any later test or project output.
    groups = re.split(rb"^[0-9T:.Z-]+ ##\[group\]Run ", log, maxsplit=2, flags=re.MULTILINE)
    _require(len(groups) == 3 and groups[1].splitlines()[0] == b"actions/checkout@v4"
             and len(groups[1]) < 128 * 1024, "CI checkout step log unavailable")
    setup, checkout, _ = groups
    _require(re.search(rb"Download action repository 'actions/checkout@v4' \(SHA:[0-9a-f]{40}\)", setup),
             "runner did not resolve the official checkout action")
    oid = synthetic.encode("ascii")
    fetch = b" origin +" + oid + f":refs/remotes/pull/{pr}/merge".encode("ascii")
    # GitHub-hosted runner timestamps precede every line on both operating systems.
    fetches = [line for line in checkout.splitlines() if b"[command]" in line and b" fetch " in line]
    _require(len(fetches) == 1 and fetches[0].endswith(fetch), "CI tested a different PR merge ref")
    reports = re.findall(rb"\[command\][^\r\n]+ log -1 --format=%H\r?\n"
                         rb"[0-9T:.Z-]+ ([0-9a-f]{40})\r?$", checkout, re.MULTILINE)
    _require(reports == [oid],
             "CI checkout did not verify the expected merge commit")


def attest(repository, revision, base, git_dir, *, gh=DEFAULT_GH):
    """Attest the next first-parent main merge in the operator-owned Git mirror."""
    _require(repository == REPOSITORY, "unsupported repository")
    _sha(revision)
    _sha(base)
    gh = Path(gh)
    _require(gh.is_absolute(), "an absolute operator-installed GitHub CLI path is required")
    git_dir = Path(git_dir).resolve(strict=True)
    if (git_dir / ".git").is_dir() and not (git_dir / ".git").is_symlink():
        git_dir = git_dir / ".git"

    def git(*args):
        return _git(git_dir, *args)

    def get(path, *, log=False):
        return _request(gh, path, log=log)

    _require(git("config", "--local", "--no-includes", "--get-all", "remote.origin.url")
             == f"https://github.com/{REPOSITORY}.git", "operator origin differs from the policy repository")
    tracked_main = "refs/remotes/origin/main"
    tip = _sha(git("rev-parse", "--verify", tracked_main))
    parents = git("rev-list", "-n1", "--parents", revision).split()
    _require(len(parents) == 3 and parents[:2] == [revision, base], "not a direct main merge from stable")
    # --is-ancestor also follows second parents. Resolve the counted first-parent
    # distance to prove the release is actually on main's first-parent lineage.
    distance = int(git("rev-list", "--first-parent", "--count", f"{revision}..{tip}"))
    _require(git("rev-parse", "--verify", f"{tip}~{distance}") == revision,
             "candidate merge is outside the first-parent main history")
    head = _sha(parents[2])
    tree = _sha(git("rev-parse", revision + "^{tree}"))
    _require(git("rev-parse", head + "^{tree}") == tree, "merged content differs from PR head")
    try:
        def main_tip():
            ref = get("git/ref/heads/main")
            _require(ref["ref"] == "refs/heads/main" and ref["object"]["type"] == "commit"
                     and ref["object"]["sha"] == tip and git("rev-parse", "--verify", tracked_main) == tip,
                     "main advanced since the trusted fetch")

        main_tip()
        linked = get(f"commits/{revision}/pulls?per_page=2")
        _require(isinstance(linked, list) and len(linked) == 1, "merge lacks one associated PR")
        pr = _id(linked[0]["number"])
        pull = get(f"pulls/{pr}")
        _require(pull["number"] == pr and pull["state"] == "closed" and pull["merged"] is True
                 and pull["merge_commit_sha"] == revision and pull["base"]["ref"] == "main" and pull["base"]["sha"] == base
                 and pull["base"]["repo"]["full_name"] == pull["head"]["repo"]["full_name"] == REPOSITORY
                 and pull["head"]["sha"] == head, "PR identity or merge differs")
        branch = pull["head"]["ref"]
        runs = _runs(get, head, branch, pr)
        jobs = {path: _jobs(get, path, runs[path], head) for path in WORKFLOWS}
        references = runs[DESKTOP][3]
        _require(isinstance(references, list) and len(references) == 1 and runs[CI][3] == [],
                 "unexpected reusable workflow")
        synthetic = _sha(references[0]["sha"])
        _require(references[0]["ref"] == f"refs/pull/{pr}/merge"
                 and references[0]["path"] == f"{REPOSITORY}/{TESTED_MERGE}@{synthetic}",
                 "installer run tested a different PR merge ref")
        commit = get(f"git/commits/{synthetic}")
        _require(commit["sha"] == synthetic and commit["tree"]["sha"] == tree
                 and [p["sha"] for p in commit["parents"]] == [base, head], "tested merge or parents differ")
        for path in (CI, DESKTOP, TESTED_MERGE):
            source = _git(git_dir, "cat-file", "blob", f"{revision}:{path}", limit=128 * 1024)
            _checkout_workflow(source)
        for group in jobs.values():
            for job in group.values():
                _checkout_evidence(get(f"actions/jobs/{job}/logs", log=True), pr, synthetic)
        _require(_runs(get, head, branch, pr) == runs, "workflow evidence changed during verification")
        main_tip()
    except (AttributeError, KeyError, TypeError, IndexError) as exc:
        raise ValueError("Malformed GitHub release evidence") from exc
    return Attestation(pr, head, revision, tree, tuple(runs[p][0] for p in WORKFLOWS),
                       tuple(sorted(name for group in jobs.values() for name in group)),
                       tuple(runs[p][1] for p in WORKFLOWS), synthetic)

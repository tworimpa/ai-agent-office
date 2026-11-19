"""Validate versioned skills and parallel run receipts from a pinned Studio peer."""
import re

from .away_expert import validate_policy
from .engines import DEFAULT_DEVELOPER, DEFAULT_REVIEWER, SUPPORTED_ENGINES
from .events import snapshot_request
from .models import ACTIVE, MAX_SQLITE_INT, TERMINAL_OUTCOMES, validate_id
from .role_skills import PAGE_SIZE, instruction_digest, normalize_name, normalize_selection
from .team_runs import aggregate, normalize_subtasks, validate_task


def _metadata(skill, *, detail=False):
    fields = {"id", "name", "version", "digest"}
    if detail:
        fields.add("instructions")
    if not isinstance(skill, dict) or skill.keys() != fields:
        raise ValueError("Invalid skill metadata")
    validate_id(skill["id"])
    if (normalize_name(skill["name"]) != skill["name"]
            or type(skill["version"]) is not int or not 1 <= skill["version"] <= MAX_SQLITE_INT
            or not isinstance(skill["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", skill["digest"])):
        raise ValueError("Invalid skill version")
    if detail and instruction_digest(skill["instructions"]) != skill["digest"]:
        raise ValueError("Skill content differs from its registered version")


def _consistent_versions(skills):
    # The same immutable version can be selected by several roles.
    by_id, by_version, by_content = {}, {}, {}
    for skill in skills:
        for index, key in ((by_id, skill["id"]), (by_version, (skill["name"], skill["version"])),
                           (by_content, (skill["name"], skill["digest"]))):
            if index.setdefault(key, skill) != skill:
                raise ValueError("Conflicting skill version identities")


def check_skill_reply(status, body, method, route, payload):
    if method == "POST":
        skill = body.get("skill")
        _metadata(skill)
        if (status != 201 or skill["name"] != normalize_name(payload.get("name"))
                or skill["digest"] != instruction_digest(payload.get("instructions"))):
            raise ValueError("Unexpected registration receipt")
        return
    if status != 200:
        raise ValueError("Unexpected skill response")
    if route == "/skills" or route.startswith("/skills/before/"):
        skills = body.get("skills")
        if not isinstance(skills, list) or len(skills) > PAGE_SIZE or "next_cursor" not in body:
            raise ValueError("Invalid skill list")
        last_version = {}
        for skill in skills:
            _metadata(skill)
            if skill["name"] in last_version and skill["version"] >= last_version[skill["name"]]:
                raise ValueError("Skill versions are out of order")
            last_version[skill["name"]] = skill["version"]
        _consistent_versions(skills)
        before = route.split("/")[-1] if route.startswith("/skills/before/") else None
        cursor = body["next_cursor"]
        if (any(s["id"] == before for s in skills)
                or (cursor is not None and (len(skills) != PAGE_SIZE or cursor != skills[-1]["id"]))):
            raise ValueError("Invalid skill page cursor")
    else:
        skill = body.get("skill")
        _metadata(skill, detail=True)
        if skill["id"] != route.split("/")[-1]:
            raise ValueError("Different skill version")


def _check_assignments(row, roles):
    versions = row.get("role_skill_snapshots", {})
    if not isinstance(versions, dict) or set(versions) != set(roles):
        raise ValueError("Missing assigned skill metadata")
    all_skills = []
    for role, ids in roles.items():
        items = versions[role]
        if not isinstance(items, list) or len(items) != len(ids):
            raise ValueError("Invalid assigned skill metadata")
        for skill, expected in zip(items, ids):
            _metadata(skill)
            if skill["id"] != expected:
                raise ValueError("Run skill versions do not match its role assignments")
        all_skills.extend(items)
    _consistent_versions(all_skills)


def _indices(indices):
    if (not isinstance(indices, list) or not indices or any(type(i) is not int or i < 0 for i in indices)
            or len(set(indices)) != len(indices)):
        raise ValueError("Invalid folder selection")
    return indices


def _check_folders(row):
    folders = row.get("folders")
    if not isinstance(folders, list) or not folders or not all(isinstance(f, str) and f for f in folders):
        raise ValueError("Missing assigned folders")
    if len(set(folders)) != len(folders):
        raise ValueError("Repeated assigned folders")
    return folders


def _check_state(row, allow_edits):
    state, outcome = row.get("state"), row.get("outcome")
    if not isinstance(state, str) or "outcome" not in row:
        raise ValueError("Missing execution state or outcome")
    if state in ACTIVE or state == "STOP_REQUESTED":
        if outcome is not None:
            raise ValueError("Unfinished run has a terminal outcome")
    elif not isinstance(outcome, str) or outcome not in TERMINAL_OUTCOMES.get(state, ()):
        raise ValueError("Outcome does not match execution state")
    if (allow_edits and state == "PLAN_ONLY") or (not allow_edits and (
            state in ("TESTING", "REVIEWING", "WAITING_USER", "READY_FOR_HUMAN_REVIEW")
            or outcome in ("TEST_FAILED", "REVIEW_FAILED", "CHANGES_REQUESTED"))):
        raise ValueError("Execution state does not match edit mode")


def _check_worker(row):
    validate_task(row.get("task"))
    if any(row.get(role) not in SUPPORTED_ENGINES for role in ("developer_engine", "reviewer_engine")):
        raise ValueError("Invalid role engine")


def _check_request(row, submitted):
    if (row["workspace_id"] != submitted.get("workspace_id") or row["allow_edits"] is not submitted.get("allow_edits")
            or row["use_inbox"] is not submitted.get("use_inbox", False)
            or row["developer_engine"] != submitted.get("developer_engine", DEFAULT_DEVELOPER)
            or row["reviewer_engine"] != submitted.get("reviewer_engine", DEFAULT_REVIEWER)):
        raise ValueError("Run did not confirm its workspace, permissions or role engines")
    validate_task(submitted.get("task"))
    if row["task"] != snapshot_request(submitted["task"]):
        raise ValueError("Run did not confirm the submitted task")


def _check_children(row, submitted):
    children = row.get("subtasks")
    if not isinstance(children, list) or not 2 <= len(children) <= 4:
        raise ValueError("Invalid subtask list")
    supplied = None
    if submitted is not None:
        if row["indices"] != _indices(submitted.get("indices")):
            raise ValueError("Different group folders")
        supplied = normalize_subtasks(submitted["subtasks"], row["indices"], row["developer_engine"], row["reviewer_engine"])
        if len(supplied) != len(children):
            raise ValueError("Different number of subtasks")
    seen = {row["id"]}
    scope = set(row["indices"])
    for pos, child in enumerate(children):
        if not isinstance(child, dict) or child.get("parent_id") != row["id"]:
            raise ValueError("Invalid child receipt")
        child_id = validate_id(child.get("id"))
        if child_id in seen:
            raise ValueError("Duplicate child run")
        seen.add(child_id)
        _check_worker(child)
        _check_state(child, row["allow_edits"])
        if not scope.issuperset(_indices(child.get("indices"))):
            raise ValueError("Subtask folders exceed the group selection")
        if supplied is not None:
            expected = supplied[pos]
            if (child["task"] != snapshot_request(expected["task"])
                    or any(child[key] != expected[key] for key in ("indices", "developer_engine", "reviewer_engine"))):
                raise ValueError("Different subtask assignment")
    stopping = row["state"] == "STOP_REQUESTED"
    if stopping and any(child["state"] in ACTIVE for child in children):
        raise ValueError("A group stop must reach every active child")
    if (row["state"], row["outcome"]) != aggregate(children, stopping):
        raise ValueError("Group state contradicts the child results")


def check_run_features(row, *, listing, submitted):
    roles = normalize_selection(row.get("role_skills", {}))
    if submitted is not None and roles != normalize_selection(submitted.get("role_skills", {})):
        raise ValueError("Run did not confirm its role skills")
    if not listing:
        _check_assignments(row, roles)
    kind = row.get("kind")
    if kind not in (None, "parallel", "subtask") or ("parent_id" in row and kind != "subtask"
            or "subtasks" in row and kind != "parallel"):
        raise ValueError("Inconsistent run kind")
    if submitted is not None and (kind == "subtask" or (kind == "parallel") != ("subtasks" in submitted)):
        raise ValueError("Run did not confirm its execution mode")
    if not roles and kind is None:
        return  # Earlier peers report only the fields available for ordinary runs.
    validate_id(row.get("workspace_id"))
    _check_worker(row)
    if type(row.get("allow_edits")) is not bool or type(row.get("use_inbox")) is not bool:
        raise ValueError("Invalid run mode")
    _check_state(row, row["allow_edits"])
    if row.get("delegation") is not None:
        validate_policy(row["delegation"])
        if not row["use_inbox"]:
            raise ValueError("Delegation requires the inbox")
    if submitted is not None:
        _check_request(row, submitted)
    if kind in ("parallel", "subtask"):
        revision = row.get("base_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision):
            raise ValueError("Missing group base revision")
    if kind == "subtask" and (listing or validate_id(row.get("parent_id")) == row["id"]):
        raise ValueError("Invalid subtask parent")
    if listing:
        return
    folders = _check_folders(row)
    if kind in ("parallel", "subtask") and len(_indices(row.get("indices"))) != len(folders):
        raise ValueError("Inconsistent assigned folders")
    if kind == "subtask":
        validate_task(row.get("original_task"))
        for field in ("workspace", "branch", "developer_output", "test_output", "review_output", "changes", "diff", "diagnostic"):
            if not isinstance(row.get(field), str):
                raise ValueError("Missing child evidence")
    if kind == "parallel":
        _check_children(row, submitted)

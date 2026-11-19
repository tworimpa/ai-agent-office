"""Exercise the pinned HTTP receipt parser with independent changes to one field."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json

import pytest

from agent_office import team_wire
from agent_office.studio_ssh import RemoteUnavailable, _read_response
from test_studio_ssh import JOURNAL, frame


PARENT = "a" * 32
WORKSPACE = "b" * 32
REVISION = "a1" * 20
ENGINES = {"developer_engine": "codex", "reviewer_engine": "antigravity"}
SKILL = {"id": "c" * 32, "name": "테스트", "version": 3, "digest": sha256(b"run tests").hexdigest()}


def read_reply(body, route, payload=None, *, code=None):
    method = "GET" if payload is None else "POST"
    code = code or (202 if method == "POST" else 200)
    stream = BytesIO(frame(json.dumps(body).encode(), code))
    return _read_response(stream, JOURNAL, method, route, payload)


@pytest.fixture
def group():
    subtasks = [{"task": "first", "indices": [1]}, {"task": "second", "indices": [0]}]
    request = {"id": PARENT, "task": "parent", "workspace_id": WORKSPACE, "indices": [1, 0],
               "allow_edits": True, "subtasks": subtasks, "role_skills": {"reviewer": [SKILL["id"]]}}
    children = [dict(child, **ENGINES, id=str(i + 1) * 32, parent_id=PARENT, state="QUEUED", outcome=None)
                for i, child in enumerate(subtasks)]
    receipt = dict(deepcopy(request), **ENGINES, use_inbox=False, kind="parallel", subtasks=children,
                   folders=["server", "client"], base_revision=REVISION, state="QUEUED", outcome=None,
                   role_skill_snapshots={"reviewer": [dict(SKILL)]})
    return request, receipt


def test_group_accepts_submission_and_reconnected_partial_failure(group):
    request, parent = group
    assert read_reply({"run": parent}, "/runs", request)[0] == 202
    parent["subtasks"][0].update(state="READY_FOR_HUMAN_REVIEW", outcome="APPROVED")
    parent["subtasks"][1].update(state="FAILED", outcome="TEST_FAILED")
    parent.update(state="FAILED", outcome="TEST_FAILED")
    assert read_reply({"run": parent}, "/runs/" + PARENT)[0] == 200
    parent.update(state="READY_FOR_HUMAN_REVIEW", outcome="APPROVED")
    with pytest.raises(RemoteUnavailable):
        read_reply({"run": parent}, "/runs/" + PARENT)


@pytest.mark.parametrize("replacement", [
    {"allow_edits": False}, {"workspace_id": "d" * 32}, {"use_inbox": True},
    {"developer_engine": "claude"}, {"reviewer_engine": "cline"}, {"indices": [0, 1]},
    {"task": "different"}, {"role_skills": {}}, {"subtasks": []},
    {"state": "STOP_REQUESTED"}, {"base_revision": ""}, {"outcome": "CANCELED"},
])
def test_group_rejects_changed_submission(group, replacement):
    request, parent = group
    parent.update(replacement)
    with pytest.raises(RemoteUnavailable):
        read_reply({"run": parent}, "/runs", request)


@pytest.mark.parametrize("change", [
    {"parent_id": "e" * 32}, {"id": PARENT}, {"id": "2" * 32},
    {"state": []}, {"state": "DONE"}, {"state": "FAILED", "outcome": "APPROVED"},
    {"indices": []}, {"indices": [5]}, {"indices": [True]}, {"indices": [0, 0]},
    {"indices": [["0"]]}, {"developer_engine": []}, {"task": {}},
])
def test_reconnected_group_rejects_invalid_child_identity_status_and_scope(group, change):
    _, parent = group
    parent["subtasks"][0].update(change)
    with pytest.raises(RemoteUnavailable):
        read_reply({"run": parent}, "/runs/" + PARENT)


def test_group_stop_is_valid_only_after_all_active_children_have_stop_markers(group):
    _, parent = group
    parent.update(state="STOP_REQUESTED")
    parent["subtasks"][0].update(state="STOP_REQUESTED")
    with pytest.raises(RemoteUnavailable):
        read_reply({"run": parent}, "/runs/" + PARENT + "/stop", {}, code=200)
    parent["subtasks"][1].update(state="CANCELED", outcome="CANCELED")
    assert read_reply({"run": parent}, "/runs/" + PARENT + "/stop", {}, code=200)[0] == 200


def test_direct_child_receipt_requires_real_parent_base_and_evidence(group):
    _, parent = group
    child = dict(parent["subtasks"][0], kind="subtask", workspace_id=WORKSPACE, allow_edits=True,
                 use_inbox=False, base_revision=REVISION, original_task=parent["task"], folders=["server"],
                 workspace="worktree", branch="worker", developer_output="", test_output="", review_output="",
                 changes="", diff="", diagnostic="")
    route = "/runs/" + child["id"]
    assert read_reply({"run": child}, route)[0] == 200
    for replacement in ({"parent_id": child["id"]}, {"base_revision": None}, {"original_task": None},
                        {"folders": []}, {"diff": {"run": PARENT}}, {"kind": None}):
        with pytest.raises(RemoteUnavailable):
            read_reply({"run": dict(child, **replacement)}, route)


def test_skill_registration_detail_and_digest_match_exact_text():
    request = {"name": " 테스트 ", "instructions": "run tests"}
    detail = dict(SKILL, instructions="run tests")
    assert read_reply({"skill": SKILL}, "/skills", request, code=201)[0] == 201
    assert read_reply({"skill": detail}, "/skills/" + SKILL["id"])[0] == 200
    for change in ({"instructions": "run tests\n"}, {"id": PARENT}, {"version": True}, {"name": " 테스트"},
                   {"name": "bad\nname"}, {"instructions": " \n", "digest": sha256(b" \n").hexdigest()}):
        with pytest.raises(RemoteUnavailable):
            read_reply({"skill": dict(detail, **change)}, "/skills/" + SKILL["id"])
    with pytest.raises(RemoteUnavailable):
        read_reply({"skill": dict(SKILL, digest="0" * 64)}, "/skills", request, code=201)


def test_skill_pages_and_snapshots_cannot_mix_revisions(group, monkeypatch):
    monkeypatch.setattr(team_wire, "PAGE_SIZE", 2)
    previous = dict(SKILL, version=2, digest="f" * 64, id="d" * 32)
    page = {"skills": [SKILL, previous], "next_cursor": previous["id"]}
    assert read_reply(page, "/skills")[0] == 200
    for rows, cursor in (([previous, SKILL], None), ([SKILL, dict(previous, version=3)], None),
                         ([SKILL], SKILL["id"]), ([SKILL, previous], SKILL["id"]),
                         ([dict(SKILL, instructions="run tests")], None)):
        with pytest.raises(RemoteUnavailable):
            read_reply({"skills": rows, "next_cursor": cursor}, "/skills")
    with pytest.raises(RemoteUnavailable):
        read_reply(page, "/skills/before/" + previous["id"])
    _, parent = group
    parent["role_skills"]["verifier"] = [SKILL["id"]]
    parent["role_skill_snapshots"]["verifier"] = [dict(SKILL, version=2)]
    with pytest.raises(RemoteUnavailable):
        read_reply({"run": parent}, "/runs/" + PARENT)

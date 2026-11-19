"""Host catalog revision, capacity and actual worker prompt contracts."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_office import role_skills as skills
from agent_office.cli import execute_run
from agent_office.models import OfficeError, OwnerInfo, TaskSpec
from agent_office.store import TaskStore
from agent_office.workspace import plan_worktree
from test_studio_runner import install_fake, repo


def test_registered_digest_is_checked_before_display_and_execution(tmp_path):
    journal = tmp_path / "journal.db"
    catalog = skills.RoleSkillRegistry(journal)
    metadata = catalog.create("review", "inspect the tests")
    run = uuid4().hex
    with TaskStore(journal) as store, store._write():
        skills.pin_selection(store.connection, run, {"reviewer": [metadata["id"]]})
        store.connection.execute("UPDATE studio_skill_versions SET instructions=? WHERE id=?",
                                 ("changed elsewhere", metadata["id"]))
    with pytest.raises(OfficeError, match="STORE_IO"):
        catalog.detail(metadata["id"])
    with TaskStore(journal, readonly=True) as store, pytest.raises(OfficeError, match="STORE_IO"):
        skills.role_guidance(store.connection, run, ("reviewer",))


def test_registration_survives_retry_revision_races_and_reopen(tmp_path):
    catalog = skills.RoleSkillRegistry(tmp_path / "journal.db")
    body = "---\r\nname: test\r\n---\r\n# 절차\r\n\t내용 유지\r\n"
    first = catalog.create(" 지침 ", body)
    assert first["version"] == 1 and first["name"] == "지침"
    assert first["digest"] == sha256(body.encode()).hexdigest()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda i: catalog.create("지침", body + str(i % 3)), range(18)))
    assert {row["version"] for row in results} == {2, 3, 4}
    assert len({row["id"] for row in results}) == 3
    reopened = skills.RoleSkillRegistry(tmp_path / "journal.db")
    assert reopened.create("지침", body) == first
    assert reopened.detail(first["id"]) == {**first, "instructions": body}
    assert len(reopened.list()) == 4
    assert all("instructions" not in row for row in reopened.list())
    other = skills.RoleSkillRegistry(tmp_path / "other.db")
    with pytest.raises(OfficeError, match="NOT_FOUND"):
        other.detail(first["id"])
    assert other.create("지침", body)["id"] != first["id"]


def test_bounded_page_survives_newer_insert_without_losing_older_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "PAGE_SIZE", 2)
    catalog = skills.RoleSkillRegistry(tmp_path / "journal")
    versions = [catalog.create("기술 문서", str(i)) for i in range(4)]
    first = catalog.page()
    assert first == {"skills": versions[3:1:-1], "next_cursor": versions[2]["id"]}
    catalog.create("기술 문서", "최신")
    assert catalog.page(first["next_cursor"]) == {"skills": versions[1::-1], "next_cursor": None}
    with pytest.raises(OfficeError, match="NOT_FOUND"):
        catalog.page(uuid4().hex)
    with pytest.raises(ValueError):
        catalog.page("invalid")


@pytest.mark.parametrize("name,body", [
    ("", "text"), ("a\nb", "text"), ("a", " \n"), ("a", "x\x00"),
    ("a" * 129, "text"), ("a", "가" * 2731), ("a", {}), ("\ud800", "text"),
])
def test_rejects_invalid_registration(tmp_path, name, body):
    catalog = skills.RoleSkillRegistry(tmp_path / "journal")
    with pytest.raises(ValueError):
        catalog.create(name, body)
    assert catalog.list() == []


def test_selection_validates_entire_request_and_bounds_before_pinning(tmp_path):
    path = tmp_path / "journal"
    catalog = skills.RoleSkillRegistry(path)
    local = catalog.create("Test", "a" * 8192)
    foreign = skills.RoleSkillRegistry(tmp_path / "other").create("Test", "a" * 8192)
    run_id = uuid4().hex
    with TaskStore(path) as store:
        for value in (None, {"tester": []}, {"developer": local["id"]}, {"developer": [True]},
                      {"reviewer": [local["id"]] * 2}, {"developer": [uuid4().hex for _ in range(5)]},
                      {"developer": [local["id"]], "reviewer": [foreign["id"]]}):
            with pytest.raises(ValueError), store._write():
                skills.pin_selection(store.connection, run_id, value)
            assert skills.selected_metadata(store.connection, run_id) == {}


def test_ordered_pinned_instructions_reach_only_matching_cli_role(tmp_path, repo, monkeypatch):
    path = tmp_path / "journal"
    catalog = skills.RoleSkillRegistry(path)
    old = catalog.create("검사", "ORIGINAL_DEVELOPER")
    second = catalog.create("개발", "SECOND_DEVELOPER")
    reviewer = catalog.create("리뷰", "ONLY_REVIEWER")
    away = catalog.create("부재", "ONLY_AWAY")
    selection = {"developer": [second["id"], old["id"]], "reviewer": [reviewer["id"]],
                 "specialist": [away["id"]], "verifier": [away["id"]]}
    calls = tmp_path / "cli-calls"
    install_fake(tmp_path, monkeypatch, f"""
import json, sys
prompt = sys.stdin.read()
with open({str(calls)!r}, "a", encoding="utf-8") as output:
    print(json.dumps(prompt), file=output)
message = "VERDICT: APPROVED" if prompt.startswith("Review the requested") else "done"
print(json.dumps({{"type":"item.completed", "item":{{"type":"agent_message","text":message}}}}))
""")
    run_id = uuid4().hex
    with TaskStore(path) as store:
        store.create_run(TaskSpec(uuid4().hex, str(repo), "확인", "codex", "codex", 1), run_id,
                         plan_worktree(repo, run_id), OwnerInfo("host", uuid4().hex),
                         register=lambda db: skills.pin_selection(db, run_id, selection))
        snapshots = skills.selected_metadata(store.connection, run_id)
        assert {role: [row["id"] for row in versions] for role, versions in snapshots.items()} == selection
        current = catalog.create("검사", "REPLACEMENT_DEVELOPER")
        assert current["version"] == 2
        args = SimpleNamespace(task="확인", repo=repo, developer="codex", reviewer="codex", allow_edits=True,
                               timeout=10, max_attempts=1, test_command=[sys.executable, "-c", "pass"])
        assert execute_run(args, store, run_id, prepared=True)["status"] == "READY_FOR_HUMAN_REVIEW"
    developer, review = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert developer.index("SECOND_DEVELOPER") < developer.index("ORIGINAL_DEVELOPER")
    assert "Never commit, push, merge, deploy" in developer and "ONLY_REVIEWER" not in developer
    assert "ONLY_REVIEWER" in review and "VERDICT: APPROVED" in review and "_DEVELOPER" not in review
    assert all("ONLY_AWAY" not in prompt and "REPLACEMENT_DEVELOPER" not in prompt for prompt in (developer, review))

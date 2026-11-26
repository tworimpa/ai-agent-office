from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import sqlite3

import pytest

from agent_office import meeting_store as meetings
from agent_office.models import OfficeError
from agent_office.store import TaskStore
from agent_office.studio_runner import SCHEMA
from test_store import RUN, seeded


DIRECTION = "c" * 32
DECISION = "d" * 32
CRITERION = "같은 요청의 결과가 복구된다."


def plan(question=False):
    return {"outcome": "작업 결과를 다시 확인한다.", "scope": ["src"], "rationale": ["기존 저장소 재사용"],
            "tasks": [{"id": "save", "description": "결과 기록", "owner": "developer", "depends_on": []}],
            "criteria": [{"criterion": CRITERION, "verification": "재시작 후 동일 요청을 조회한다."}],
            "questions": [{"question": "결과를 어떻게 표시할까요?", "options": ["목록", "표"]}] if question else [],
            "evidence": {"revision": "a" * 40, "paths": ["src/store.py"]}}


def setup_meeting(path, review_plan=False, *, coordinator_plan=False):
    with seeded(path) as store, store._write():
        db = store.connection
        db.execute(SCHEMA)
        db.execute("""INSERT INTO studio_runs(id,workspace_id,folders,allow_edits,request_hash)
            VALUES (?,'workspace','["src"]',1,'hash')""", (RUN,))
        meetings.ensure_schema(db)
        meetings.register(db, RUN, review_plan, coordinator_plan=coordinator_plan)


@contextmanager
def write(path):
    with TaskStore(path, existing=True) as store, store._write():
        yield store.connection


def decision(p, **changes):
    return {"id": DECISION, "revision": 1, "plan": deepcopy(p), "answers": {}, **changes}


def test_persisted_rounds_product_decisions_and_operator_changes(tmp_path):
    path = tmp_path / "회의.sqlite"
    setup_meeting(path)
    with write(path) as db:
        for role, engine, text in [("developer", "codex", "구현 제안"), ("reviewer", "antigravity", "수정 요청"),
                                   ("developer", "codex", "수정안"), ("reviewer", "antigravity", "검토 완료")]:
            meetings.contribute(db, RUN, role, engine, text)
        assert meetings.propose(db, RUN, plan(True))["status"] == "awaiting_review"
    with TaskStore(path, readonly=True) as store:
        detail = meetings.describe(store.connection, RUN)
    assert [item["text"] for item in detail["contributions"]] == ["구현 제안", "수정 요청", "수정안", "검토 완료"]
    assert detail["choices"] == [dict(plan(True)["questions"][0], id="q1")]
    edited = dict(plan(True), outcome="검증 결과를 복원한다.")
    payload = decision(edited, answers={"q1": "표"})
    with write(path) as db:
        for invalid in (decision(edited), dict(payload, answers={"q1": "임의 답변"}),
                        dict(payload, permission="approve"), dict(payload, plan=dict(edited, evidence={"revision": "b"*40, "paths": []})),
                        dict(payload, plan=dict(edited, questions=[]), answers={})):
            with pytest.raises(ValueError):
                meetings.decide(db, RUN, invalid)
        approved = meetings.decide(db, RUN, payload)
        assert meetings.decide(db, RUN, payload) == approved
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            meetings.decide(db, RUN, dict(payload, answers={"q1": "목록"}))
    assert approved["status"] == "approved" and approved["revision"] == 2
    assert approved["answers"] == {"q1": "표"} and approved["decision"] == {"id": DECISION, "revision": 1}
    draft, published = approved["revisions"]
    assert draft["plan"] == plan(True) and draft["approved_by"] is None
    assert published["plan"] == edited and published["approved_by"] == "operator"
    assert published["verification"] == [{"criterion": CRITERION, "status": "not_verified", "evidence": "검증 실행 전입니다."}]


def test_direction_rechecks_previous_results_and_preserves_each_round(tmp_path):
    path = tmp_path / "db"
    setup_meeting(path)
    initial = plan(True)
    opinions = [{"role": "developer", "engine": "codex", "text": "지시 검토"}]
    with write(path) as db:
        meetings.propose(db, RUN, initial)
        meetings.decide(db, RUN, decision(initial, answers={"q1": "표"}))
        meetings.verify(db, RUN, 1, CRITERION, "passed", "복원 테스트에서 요청 ID와 결과가 일치했다.")
        edited = dict(initial, rationale=["추가 지시로 표시 순서 변경"])
        args = db, RUN, 1, edited, "표시 순서를 수정", DIRECTION, opinions
        revised = meetings.revise(*args)
        assert meetings.revise(*args) == revised
        assert revised["contributions"][-1]["revision"] == 2
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            meetings.revise(*args[:-1], [])
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            meetings.verify(db, RUN, 1, CRITERION, "passed", "이전 결과")
        with pytest.raises(ValueError):
            meetings.verify(db, RUN, 2, CRITERION, "passed", "")
        assert revised["revisions"][0]["verification"][0]["status"] == "passed"
        assert revised["revisions"][1]["direction_id"] == DIRECTION
        assert revised["verification"][0]["status"] == "not_verified" and revised["answers"] == {"q1": "표"}
        assert revised["status"] == "approved"
        meetings.verify(db, RUN, 2, CRITERION, "failed", "TOKEN=sensitive 실제 결과가 비어 있음")
        assert "sensitive" not in str(meetings.describe(db, RUN)["verification"])
        changed_question = deepcopy(edited)
        changed_question["questions"][0]["options"] = ["표", "CSV"]
        next_draft = meetings.revise(db, RUN, 2, changed_question, "선택지 추가", "e"*32)
        assert next_draft["status"] == "awaiting_review" and next_draft["answers"] == {}


def test_decision_concurrency_and_restarted_reader(tmp_path):
    path = tmp_path / "db"
    setup_meeting(path, True)
    with write(path) as db:
        meetings.propose(db, RUN, plan())
    def submit(key):
        with write(path) as db:
            try:
                return meetings.decide(db, RUN, decision(plan(), id=key))["status"]
            except OfficeError as exc:
                return exc.code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(submit, ["1"*32, "2"*32])) == ["REQUEST_CLOSED", "approved"]
    with TaskStore(path, readonly=True) as store:
        assert meetings.summary(store.connection, RUN) == {"status": "approved", "revision": 1, "error": None,
                                                          "coordinator_plan": False}
        assert meetings.describe(store.connection, "b"*32) is None


@pytest.mark.parametrize("operation", ["stop", "recover", "stop_requested", "finish_pending"])
def test_pending_review_cannot_resume_stale_or_stopped_run(tmp_path, operation):
    path = tmp_path / "db"
    setup_meeting(path, True)
    with write(path) as db:
        meetings.propose(db, RUN, plan())
        if operation == "stop":
            meetings.stop(db, RUN)
        elif operation == "recover":
            db.execute("UPDATE runs SET state='RECOVERY_REQUIRED',ended_at='done' WHERE run_id=?", (RUN,))
        elif operation == "stop_requested":
            db.execute("UPDATE studio_runs SET stop_requested=1 WHERE id=?", (RUN,))
        else:
            db.execute("UPDATE studio_runs SET proposed_state='PLAN_ONLY' WHERE id=?", (RUN,))
        with pytest.raises(OfficeError, match="REQUEST_CLOSED"):
            meetings.decide(db, RUN, decision(plan()))
        assert meetings.describe(db, RUN)["decision"] is None


def test_decision_failure_rolls_back_and_scope_is_bound_to_original_run(tmp_path):
    path = tmp_path / "db"
    setup_meeting(path, True)
    with write(path) as db:
        meetings.propose(db, RUN, plan())
        outside = plan()
        outside["tasks"][0]["folders"] = ["different"]
        with pytest.raises(ValueError):
            meetings.decide(db, RUN, decision(outside))
        db.execute("""CREATE TRIGGER fail_decision BEFORE INSERT ON studio_meeting_decisions
            BEGIN SELECT RAISE(ABORT,'disk full'); END""")
    with pytest.raises(sqlite3.IntegrityError), write(path) as db:
        meetings.decide(db, RUN, decision(dict(plan(), outcome="편집")))
    with write(path) as db:
        detail = meetings.describe(db, RUN)
        assert (detail["status"], detail["revision"], detail["decision"]) == ("awaiting_review", 1, None)
        meetings.fail(db, RUN, "TOKEN=private 회의 중단")
        meetings.stop(db, RUN)
        assert meetings.summary(db, RUN) == {"status": "failed", "revision": 1, "error": "TOKEN=[redacted] 회의 중단",
                                             "coordinator_plan": False}


def test_unreviewed_plan_does_not_imply_human_approval_or_verification(tmp_path):
    path = tmp_path / "db"
    setup_meeting(path)
    with write(path) as db:
        result = meetings.propose(db, RUN, plan())
        assert result["status"] == "approved" and result["decision"] is None
        assert result["revisions"][0]["approved_by"] == "coordinator"
        assert result["verification"][0]["status"] == "not_verified"


def test_operator_cannot_remove_independent_review_from_edit_plan(tmp_path):
    path = tmp_path / "db"
    setup_meeting(path, True, coordinator_plan=True)
    with write(path) as db:
        proposal = plan()
        proposal["tasks"].append({"id": "review", "description": "변경 확인", "owner": "reviewer",
                                  "depends_on": ["save"]})
        assert meetings.propose(db, RUN, proposal)["status"] == "awaiting_review"
        with pytest.raises(ValueError, match="독립 리뷰"):
            meetings.decide(db, RUN, decision(plan()))
        assert meetings.describe(db, RUN)["decision"] is None


def test_migration_preserves_old_plan_authoring_mode(tmp_path):
    path = tmp_path / "db"
    setup_meeting(path, True)
    with write(path) as db:
        db.execute("ALTER TABLE studio_meetings DROP COLUMN coordinator_plan")
        meetings.ensure_schema(db)
        assert meetings.describe(db, RUN)["coordinator_plan"] is False
        assert meetings.propose(db, RUN, plan())["status"] == "awaiting_review"

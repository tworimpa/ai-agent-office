from contextlib import contextmanager
from uuid import uuid4

import pytest

from agent_office import workspace_coordination as c
from agent_office.store import TaskStore, encode, now
from agent_office.studio_ssh import _check_reply, is_peer_route
from test_studio_http import project, request, studio
from test_workspace_rooms import engine_snapshot


@contextmanager
def office(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    with studio(tmp_path/"office.db") as server:
        monkeypatch.setattr(server.runner,"_start_monitor",lambda: None)
        monkeypatch.setattr(server.engines,"snapshot",lambda:engine_snapshot("codex","antigravity"))
        ws = request(server,"/api/workspaces",{"name":"office","paths":[str(web.parent)]})[1]["workspace"]["id"]
        base = "/api/rooms/" + ws
        def assign(task, coordination=None):
            data = dict(id=uuid4().hex,message=task,allow_edits=True,delegation=False,goal=True,
                        goal_criteria=["동작 확인"])
            if coordination is not None:
                data["coordination"] = coordination
            status,body = request(server,base+"/assignments",data)
            assert status == 202,body
            return data["id"]
        yield server, ws, base, assign


def add_request(db, rid, *, product=True, prompt="이전 설정을 변환할까요?"):
    req = uuid4().hex
    db.execute("""INSERT INTO studio_requests(id,run_id,role,engine,attempt,holder,session_hash,kind,
        decision_type,prompt,status,created_at) VALUES(?,?,'developer','codex',1,?,?,?, ?,?,'pending',?)""",
               (req,rid,uuid4().hex,"test","question" if product else "permission",
                "product_decision" if product else None,prompt,now()))
    return req


def test_decision_gates_just_the_selected_stage_and_revisions_invalidate_old_evidence(tmp_path,monkeypatch):
    with office(tmp_path,monkeypatch) as (server,ws,base,assign):
        first = assign("#41 schema · settings.py")
        with TaskStore(server.db_path,existing=True) as store:
            db = store.connection
            question = add_request(db,first)
            permission = add_request(db,first,product=False,prompt="settings.py 한 파일 편집")
            edge = dict(source_run=first,stage="implement",condition="product_decision",request_id=question,reason="저장 형식")
            second = assign("#42 settings.py",dict(priority=7,dependencies=[edge]))
            third = assign("#43 repro.py")
            board = request(server,base+"/briefing")[1]
            assert set(row["request_id"] for row in board["decisions"]) == {permission,question}
            product = next(row for row in board["decisions"] if row["request_id"] == question)
            assert {row["run_id"] for row in product["impacts"]} == {first,second}
            assert len(next(row for row in board["decisions"] if row["request_id"] == permission)["impacts"])==1
            assert board["overlaps"][0]["area"] == "settings.py"
            assert c.try_stage(db,second,"reproduce",1)["ready"]
            assert not c.try_stage(db,second,"implement",1)["ready"]
            assert c.try_stage(db,third,"implement",1)["ready"]
            for invalid in (dict(edge,source_run=second),dict(edge,request_id=permission)):
                status,_= request(server,base+"/coordination/"+first,dict(revision=1,dependencies=[invalid]))
                assert status == 400
            db.execute("UPDATE studio_requests SET status='delivered',decision='answer',answer='변환',decided_at=?,delivered_at=? WHERE id=?",
                       (now(),now(),question))
            before = c.try_stage(db,second,"implement",1)
            assert before["ready"] and before["decisions"][0]["answer"] == "변환"
            assert c.try_stage(db,second,"implement",1) == before
            duplicate = add_request(db,second)
            assert c.reuse_product_answer(db,second,1,duplicate,"이전 설정을 변환할까요?")
            target_permission = add_request(db,second,product=False)
            assert not c.reuse_product_answer(db,second,1,target_permission,"이전 설정을 변환할까요?")
            revision = dict(id=uuid4().hex,revision=1,answer="그대로 유지",reason="기존 호환성 유지")
            url=base+"/decision-revisions/"+question
            resp=request(server,url,revision)
            assert resp[0]==200 and request(server,url,revision)==resp
            assert request(server,url,dict(revision,id=uuid4().hex))[0] == 409
            assert request(server,base+"/decision-revisions/"+duplicate,revision)[0] == 409
            assert c.stage_status(db,second,"review",1)["rework_required"]
            followup = add_request(db, second)
            assert c.reuse_product_answer(db, second, 1, followup, "이전 설정을 변환할까요?")
            assert db.execute("SELECT answer FROM studio_requests WHERE id=?", (followup,)).fetchone()[0]=="그대로 유지"
            assert c.try_stage(db,second,"implement",2)["decisions"][0]["revision"]==2
            assert c.try_stage(db,second,"verify",2)["ready"]
            board=request(server,base+"/briefing")[1]
            assert question in [r["request_id"] for r in board["decision_options"]]
            assert duplicate not in [r["request_id"] for r in board["decision_options"]]
            _check_reply(200,board,[server.journal_id.encode()],server.journal_id,"GET",base[4:]+"/briefing",None)
            assert not is_peer_route("POST",base[4:]+"/briefing")


def test_corrected_legacy_decision_invalidates_transitive_completion(tmp_path, monkeypatch):
    with office(tmp_path, monkeypatch) as (server, ws, base, assign):
        source = assign("original settings decision")
        def after(run_id):
            return dict(dependencies=[dict(source_run=run_id, stage="start",
                                           condition="completion", reason="verified result")])
        middle = assign("dependent settings verifier", after(source))
        downstream = assign("publish verification summary", after(middle))
        with TaskStore(server.db_path, existing=True) as store:
            db = store.connection
            question = add_request(db, source)
            db.execute("UPDATE studio_requests SET status='delivered',decision='answer',answer='convert' WHERE id=?",
                       (question,))
            db.execute("DELETE FROM workspace_coordination_history WHERE run_id=?", (source,))
            db.execute("DELETE FROM workspace_coordination WHERE run_id=?", (source,))
            for run_id in (source, middle):
                db.execute("UPDATE runs SET state='READY_FOR_HUMAN_REVIEW',attempts=2 WHERE run_id=?", (run_id,))
            assert c.stage_status(db, downstream, "start", 1)["ready"]
            revision = dict(id=uuid4().hex, revision=1, answer="preserve", reason="preserve old settings")
            assert request(server, base+"/decision-revisions/"+question, revision)[0] == 200
            assert c.stage_status(db, source, "review", 2)["rework_required"]
            assert not c.stage_status(db, middle, "review", 2)["ready"]
            assert not c.stage_status(db, downstream, "start", 1)["ready"]


def test_completion_cycle_is_atomic_and_shared_paths_never_gate_an_independent_goal(tmp_path,monkeypatch):
    with office(tmp_path,monkeypatch) as (server,ws,base,assign):
        first=assign("#41 schema settings.py")
        second=assign("#42 Windows settings.py",dict(dependencies=[dict(source_run=first,stage="verify",
                        condition="completion",reason="변환 후 회귀")]))
        last=assign("#43 settings.py")
        back=dict(source_run=second,stage="start",condition="completion",reason="되돌아가는 간선")
        assert request(server,base+"/coordination/"+first,dict(revision=1,dependencies=[back]))[0]==400
        with TaskStore(server.db_path,existing=True) as store:
            db=store.connection
            assert c.config(db,first)["dependencies"] == []
            assert c.stage_status(db,last,"verify",1)["ready"]
            assert c.stage_status(db,second,"implement",1)["ready"]
            assert not c.stage_status(db,second,"verify",1)["ready"]
            db.execute("UPDATE runs SET state='CANCELED',outcome='CANCELED',ended_at=? WHERE run_id=?", (now(),first))
            assert "중단" in c.stage_status(db,second,"verify",1)["reasons"][0]


def test_structured_issue_metadata_resolves_same_workspace_provider_without_guessing(tmp_path,monkeypatch):
    with office(tmp_path,monkeypatch) as (server,ws,base,assign):
        first=assign("#41 schema")
        with TaskStore(server.db_path,existing=True) as store:
            db=store.connection
            db.execute("INSERT INTO studio_external_origins VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (first,first,None,"https://example.org",71,1234,41,"https://example.org/o/r/issues/41","a"*40,now()))
        md="## 작업 조정\n- 우선순위: 4\n- 변경 영역: schema\n- 선행: #41 | 검증 | 완료 | 스키마 확정"
        second=assign("#42 일치 확인\n"+md)
        with TaskStore(server.db_path,existing=True) as store:
            result=c.config(store.connection,second)
            assert result["areas"] == ["schema"] and result["priority"]==4
            assert result["dependencies"][0]["source_run"]==first
        assert c.markdown_settings("\n```text\n"+md+"\n```") is None
        assert c.markdown_settings("````md\n```\n"+md+"\n```\n````") is None


def test_same_numbered_plan_questions_keep_both_origins_and_reused_answers_keep_version(tmp_path,monkeypatch):
    with office(tmp_path,monkeypatch) as (server,_,base,assign):
        sources = [assign("설정 #41"), assign("내보내기 #44")]
        with TaskStore(server.db_path,existing=True) as store:
            db = store.connection
            for rid, prompt in zip(sources,["변환할까요?","내보낼까요?"]):
                plan = {"questions":[{"question":prompt,"options":["예","아니요"]}]}
                db.execute("""INSERT INTO studio_meetings(run_id,review_plan,status,revision,error)
                              VALUES(?,1,'awaiting_review',1,NULL)""",(rid,))
                db.execute("INSERT INTO studio_meeting_revisions VALUES(?,1,?,'coordinator','초안',NULL,'{}',NULL,?,NULL)",
                           (rid,encode(plan),now()))
            consumer=assign("복원",dict(dependencies=[
                dict(source_run=sources[0],stage="implement",condition="product_decision",question="변환할까요?",reason="저장"),
                dict(source_run=sources[1],stage="implement",condition="product_decision",request_id="plan:1:q1",reason="출력")]))
            for row in request(server,base+"/briefing")[1]["decisions"]:
                assert consumer in [link["run_id"] for link in row["impacts"]]
            for rid in sources:
                db.execute("UPDATE studio_meeting_revisions SET answers=?,approved_by='operator',approved_at=? WHERE run_id=?",
                           (encode({"q1":"예"}),now(),rid))
                db.execute("UPDATE studio_meetings SET status='approved' WHERE run_id=?",(rid,))
            assert len(c.try_stage(db,consumer,"implement",1)["decisions"])==2
            # An unrelated plan edit retains the original choice.
            source=sources[0]
            db.execute("""INSERT INTO studio_meeting_revisions SELECT run_id,2,plan,'direction',reason,NULL,
                       answers,'coordinator',created_at,approved_at FROM studio_meeting_revisions WHERE run_id=?""",(source,))
            db.execute("UPDATE studio_meetings SET revision=2 WHERE run_id=?",(source,))
            assert c.stage_status(db,consumer,"review",1)["ready"]


def test_completion_graph_reuses_shared_predecessor_checks(tmp_path,monkeypatch):
    with office(tmp_path,monkeypatch) as (server,ws,base,assign):
        chain=[]
        for i in range(18):
            deps=[dict(source_run=rid,stage="implement",condition="completion",reason="계약 확인") for rid in chain[-2:]]
            chain.append(assign(str(i), dict(dependencies=deps)))
        with TaskStore(server.db_path,existing=True) as store:
            db=store.connection
            db.execute("UPDATE runs SET state='READY_FOR_HUMAN_REVIEW',outcome='APPROVED',ended_at=?",(now(),))
            queries=[]
            db.set_trace_callback(queries.append)
            assert c.stage_status(db,chain[-1],"implement",1)["ready"]
            assert len(queries) < 500

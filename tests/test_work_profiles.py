from uuid import uuid4

import pytest

from agent_office import goal_workers, work_profiles
from agent_office.store import TaskStore, encode, now
from agent_office.studio_ssh import RemoteUnavailable, _check_reply
from test_studio_http import request
from test_workspace_briefing import office
from test_remote_studio import LoopbackLink, RemoteHost
from test_studio_http import studio


@pytest.mark.parametrize("default,override,edits,goal,confirm,expected", [
    ("auto", None, False, False, False, False),
    ("balanced", "quick", False, False, False, False),
    ("quick", None, True, True, False, False),
    ("quick", "deep", False, False, False, True),
    ("auto", None, True, False, False, True),
    ("quick", None, False, False, True, True),
])
def test_coordinator_routes_before_assigning_reviewers(tmp_path,monkeypatch,default,override,edits,goal,confirm,expected):
    with office(tmp_path,monkeypatch) as (server,ws,base,_):
        available(server.rooms.models)
        monkeypatch.setattr(server.runner,"_launch",lambda *_: None)
        assert request(server,base+"/work-policy",{"mode":default})[0] == 200
        body = dict(id=uuid4().hex,message="프로젝트 설정 확인",allow_edits=edits,delegation=False,
                    meeting="auto",goal=goal,review_plan=confirm)
        if override:
            body["work_profile"]={"mode":override}
        status,data = request(server,base+"/assignments",body)
        assert status == 202,data
        assigned = data["run"]["assignment"]
        assert bool(assigned.get("meeting")) is expected
        assert bool(assigned.get("review_plan")) is confirm
        if goal:
            assert assigned["planning_mode"] == ("meeting" if expected else "direct")
            assert assigned["goal_policy"]["checks"]
        _check_reply(202,data,[server.journal_id.encode()],server.journal_id,"POST",base[4:]+"/assignments",body)
        assert request(server,base+"/work-policy",{"mode":"deep"})[0] == 200
        assert request(server,base+"/assignments",body)[1]["run"]["assignment"] == assigned


def available(catalog):
    catalog._expires = float("inf")
    catalog._snapshot = {"engine":"codex","status":"available","refreshed_at":now(),"models":[
        {"id":"fixture","label":"Fixture","efforts":["low","medium","high"],"default_effort":"medium","is_default":True}]}


def test_quick_readonly_dispatch_runs_one_provider_call(tmp_path,monkeypatch):
    from test_goal_usage import install_fixture
    from test_studio_http import project, wait_for
    from test_workspace_rooms import engine_snapshot, register

    repo, _ = project(tmp_path)
    control = install_fixture(tmp_path, monkeypatch)
    (control / "grant").touch()
    with studio(tmp_path/"direct.db") as server:
        available(server.rooms.models)
        monkeypatch.setattr(server.engines,"snapshot",lambda:engine_snapshot("codex"))
        ws = register(server,[repo.parent])
        base = f"/api/rooms/{ws}"
        rid = uuid4().hex
        body = dict(id=rid,message="설정 파일의 기본값을 읽어 주세요.",allow_edits=False,delegation=False,meeting="auto")
        status, response = request(server,base+"/assignments",body)
        assert status == 202, response
        run = wait_for(server,rid,{"PLAN_ONLY","FAILED"})
        assert run["state"] == "PLAN_ONLY",run["diagnostic"]
        detail = request(server,base+"/usage/"+rid)[1]
        assert detail["active_roles"] == ["developer"]
        assert [(c["role"],c["stage"]) for c in detail["usage"]["calls"]] == [("developer","implement")]
        assert len((control/"turns").read_text().splitlines()) == 1
        assert not run.get("meeting") and not run["allow_edits"]


def test_settings_inheritance_host_validation_and_shared_visit_replay(tmp_path, monkeypatch):
    with office(tmp_path, monkeypatch) as (server, ws, base, assign):
        available(server.rooms.models)
        original = request(server,base+"/work-policy")[1]["policy"]
        assert original == work_profiles.defaults()
        change = dict(original,mode="quick",roles={"developer":{"model":"fixture","account_alias":"lab"}},
                      token_budget=20000,retry_deep=True)
        code, saved = request(server,base+"/work-policy",change)
        assert code == 200, saved
        assert request(server,base+"/work-policy",dict(change,roles={"developer":{"model":"not-offered"}}))[0] == 400
        assert request(server,base+"/work-policy",dict(change,fallbacks={"developer":[
            dict(engine="codex",model="fixture",account_alias="different-account")]}))[0] == 400
        rid = assign("새 테스트로 원인 재현")
        detail = request(server,base+"/usage/"+rid)[1]
        assert detail["profile"]["roles"]["developer"] == dict(model="fixture",effort="low",account_alias="lab")
        assert detail["profile"]["retry_effort"] == "high" and detail["budget"]["token_budget"] == 20000
        assert detail["usage"]["total_tokens"] is None
        change["mode"] = "deep"
        assert request(server,base+"/work-policy",change)[0] == 200
        assert request(server,base+"/usage/"+rid)[1]["profile"] == detail["profile"]
        first,second = (dict(id=uuid4().hex) for _ in range(2))
        visit = request(server,base+"/visit",first)
        assert visit[0] == 200 and visit[1]["previous_visit"] is None
        assert request(server,base+"/visit",second)[1]["previous_visit"] == visit[1]["visited_at"]
        assert request(server,base+"/visit",first) == visit
        journal = server.journal_id
        _check_reply(200,detail,[journal.encode()],journal,"GET",base[4:]+"/usage/"+rid,None)
        with pytest.raises(RemoteUnavailable):
            _check_reply(200,dict(detail,run_id=uuid4().hex),[journal.encode()],journal,"GET",base[4:]+"/usage/"+rid,None)


def test_six_axes_have_run_scoped_receipts_and_minimum_sample_size(tmp_path, monkeypatch):
    with office(tmp_path, monkeypatch) as (server,ws,base,assign):
        available(server.rooms.models)
        path = base+"/characters/developer"
        empty = request(server,path)[1]["character"]
        assert len(empty["axes"]) == 6 and all(a["level"] is None and not a["samples"] for a in empty["axes"])
        ids = [assign("재현하고 수정해주세요") for _ in range(3)]
        with TaskStore(server.db_path, existing=True) as store, store._write():
            db = store.connection
            from agent_office.goal_verification import init_schema
            init_schema(db)
            for rid in ids:
                digest = "a"*64
                record = {"files":["settings.py"],"eligible":True,"status":"passed","digest":digest}
                db.execute("INSERT INTO studio_goal_candidates VALUES(?,1,?,'fixture patch')",(rid,encode(record)))
                db.execute("INSERT INTO studio_goal_reviews VALUES(?,1,'review',?,'approved','review',?)",(rid,digest,now()))
                db.execute("INSERT INTO studio_goal_checks VALUES(?,0,1,?,'{}')",
                           (rid,encode({"failed_ids":["test_saved_theme"],"status":"failed"})))
        for role in ["developer","reviewer","coordinator"]:
            person = request(server,base+"/characters/"+role)[1]["character"]
            axes = {axis["id"]:axis for axis in person["axes"]}
            if role == "developer":
                assert axes["analysis"]["level"] == axes["implementation"]["level"] == 4
                assert {fact["run_id"] for fact in axes["analysis"]["evidence"]} == set(ids)
                assert all(f["position"] == 1 and f["attempt"] == 0 for f in axes["analysis"]["evidence"])
            if role == "reviewer":
                assert axes["review"]["samples"] == 3 and axes["implementation"]["level"] is None
            assert axes["project"]["level"] is None


def test_retry_effort_requires_delegation_measurement_and_next_hypothesis(tmp_path,monkeypatch):
    with office(tmp_path,monkeypatch) as (server,ws,base,assign):
        available(server.rooms.models)
        rid = assign("중복 실패 분석")
        with TaskStore(server.db_path,existing=True) as store, store._write():
            from agent_office.goal_verification import init_schema
            db=store.connection
            init_schema(db)
            db.execute("INSERT INTO studio_goal_checks VALUES(?,1,1,?,'{}')",
                       (rid,encode(dict(status="failed",still_failing=["test_theme"]))))
            db.execute("INSERT INTO studio_goal_candidates VALUES(?,1,?,'patch')",
                       (rid,encode(dict(files=["settings.py"],hypothesis="다음 가설: settings.py의 BOM 처리 순서를 확인한다."))))
            pref = dict(engine="codex",model="fixture",effort="low")
            profile = dict(retry_deep=True,retry_effort="high")
            assert goal_workers.retry_choice(db,rid,"implement",2,pref,{}) == pref
            assert goal_workers.retry_choice(db,rid,"implement",2,pref,profile)["effort"] == "high"
            assert goal_workers.retry_choice(db,rid,"implement",2,pref,profile)["effort"] == "high"
            assert db.execute("SELECT count(*) FROM studio_usage_actions WHERE action='retry_effort'").fetchone()[0] == 1
            assert goal_workers.retry_choice(db,rid,"implement",3,pref,profile) == pref
            assert "test_theme" in db.execute("SELECT reason FROM studio_usage_actions WHERE action='retry_effort'").fetchone()[0]


def test_remote_reads_same_policy_usage_and_character_with_separate_visit_history(tmp_path,monkeypatch):
    from test_studio_http import project
    from test_workspace_rooms import engine_snapshot, register
    with studio(tmp_path/"peer.db",accept_remote=True) as peer:
        monkeypatch.setattr(peer.runner,"_start_monitor",lambda:None)
        monkeypatch.setattr(peer.engines,"snapshot",lambda:engine_snapshot("codex","antigravity"))
        available(peer.rooms.models)
        host = RemoteHost("fixture-peer",peer.server_port,peer.journal_id)
        link = LoopbackLink(peer,host)
        monkeypatch.setattr("agent_office.studio_server.remote_request",link)
        with studio(tmp_path/"local.db",remotes=(host,)) as local:
            folder,_ = project(tmp_path)
            ws = register(peer,[folder.parent])
            direct=f"/api/rooms/{ws}"
            remote=f"/api/hosts/{peer.journal_id}/rooms/{ws}"
            ident=uuid4().hex
            data=dict(id=ident,message="설정 재현",allow_edits=True,delegation=False,goal=True)
            assert request(local,remote+"/assignments",data)[0] == 202
            for suffix in ("/usage/"+ident,"/characters/developer","/work-policy","/models"):
                status,body=request(local,remote+suffix)
                observed=request(peer,direct+suffix)[1]
                if suffix.startswith("/usage/"):
                    assert body["budget"].pop("active_used") <= observed["budget"].pop("active_used")
                assert status == 200 and body == observed
            key=dict(id=uuid4().hex)
            assert request(local,remote+"/visit",key) == request(peer,direct+"/visit",key)
            assert request(local,"/api/workspaces")[1]["workspaces"] == []
            link.online=False
            assert request(local,remote+"/usage/"+ident)[0] == 503

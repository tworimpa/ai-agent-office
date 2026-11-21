from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import sys
import time
from uuid import uuid4

import pytest

from agent_office import goal_recovery, goal_stage_wait, goal_usage as usage, goal_workers as workers
from agent_office.goal_verification import default_policy
from agent_office.store import now
from agent_office.studio_runner import LocalRunner
from test_goal_recovery import goal


def rate(key, percent, allowed=None):
    event = {"kind":"rate_limit","limit_id":"codex","windows":[{"name":"primary","used_percent":percent,
                                                               "resets_at":1,"window_minutes":300}]}
    if key:
        event["account_key"] = key
    if allowed is not None:
        event["ordinary_usage_allowed"] = allowed
    return event


def test_only_typed_provider_usage_counts_once_and_unknown_is_visible(goal):
    _,store,run_id,_ = goal
    db = store.connection
    ident = usage.begin(db,run_id,"developer","implement",1,{"engine":"codex","effort":"low"})
    other = usage.begin(db,run_id,"reviewer","review",1,{"engine":"antigravity"})
    for n in (15,15,3,23):
        usage.observe(db,ident,{"kind":"usage","input_tokens":n-2,"output_tokens":2,"total_tokens":n,
                                "context_used":4,"context_window":100,"prompt":"PRIVATE-FIXTURE"})
    usage.observe(db,ident,{"kind":"model","actual_model":"model-1","actual_effort":"high"})
    usage.observe(db,ident,{"kind":"model","actual_model":"model-2"})
    report = usage.describe(db,run_id)
    assert "PRIVATE-FIXTURE" not in str(report)
    assert (report["total_tokens"],report["unknown_calls"],report["usage_coverage"]) == (23,1,"partial")
    calls = {row["id"]:row for row in report["calls"]}
    assert calls[ident]["actual_effort"] is None and calls[ident]["requested_effort"] == "low"
    assert calls[ident]["actual_model"] == "model-2" and calls[ident]["context_window"] == 100
    assert calls[other]["total_tokens"] is None
    before = calls[ident]["observed_at"]
    usage.observe(db,ident,{"kind":"usage","total_tokens":3,"context_used":1})
    stale = next(c for c in usage.describe(db,run_id)["calls"] if c["id"] == ident)
    assert (stale["context_used"],stale["observed_at"]) == (4,before)
    assert [item["action"] for item in report["decisions"]] == ["provider_change","provider_change"]
    db.execute("UPDATE studio_usage_calls SET finished_at=?",(now(),))
    assert workers.budget(db,run_id,{"token_budget":30})[0] == "TOKEN_BUDGET_UNVERIFIED"
    usage.observe(db,other,{"kind":"usage","total_tokens":7})
    assert workers.budget(db,run_id,{"token_budget":30})[0] == "TOKEN_BUDGET_EXHAUSTED"


def test_shared_quota_never_sums_host_snapshots_or_guesses_identity(goal):
    _,store,run_id,_ = goal
    db = store.connection
    first,second = "a"*64,"b"*64
    choice = dict(engine="codex",account_alias="team")
    other = uuid4().hex
    def record(run,origin,key,used,allowed,alias="team"):
        usage.record_rates(db,run,"developer","codex",alias,origin,rate(key,used,allowed),"metadata_probe")
    record(run_id,"one",first,100,False)
    record(other,"two",second,1,True)
    assert usage.blockers(db,run_id,"developer",choice)
    record(other,"two",first,1,None)
    assert usage.blockers(db,run_id,"developer",choice)
    secondary = rate(first,1,True)
    secondary["windows"][0]["name"] = "secondary"
    usage.record_rates(db,other,"developer","codex","team","two",secondary,"metadata_probe")
    assert usage.blockers(db,run_id,"developer",choice)
    record(other,"two",first,1,True)
    assert not usage.blockers(db,run_id,"developer",choice)
    assert len([r for r in usage.describe(db,run_id)["rate_limits"] if r["name"]=="primary"
                and r["pool_id"]==first]) == 1
    record(run_id,"three",None,100,None,None)
    record(run_id,"four",None,0,True,None)
    assert usage.blockers(db,run_id,"developer",dict(engine="codex"))
    assert not usage.blockers(db,other,"reviewer",dict(engine="codex"))
    record(run_id,"account-a",None,100,False,"declared")
    record(other,"account-b",None,0,True,"declared")
    assert not usage.blockers(db,run_id,"developer",dict(engine="codex",account_alias="declared"))


def test_only_the_designated_role_can_use_a_preapproved_alternative(goal):
    _,store,rid,_=goal
    db=store.connection
    engines={"developer":"codex","reviewer":"antigravity"}
    profile={"roles":{"developer":{"account_alias":"team"}},"fallbacks":{"developer":[]}}
    usage.record_rates(db,rid,"developer","codex","team","source",rate("a"*64,100,False),"provider_event")
    assert workers.select(db,rid,"developer",profile,engines)[0] is None
    profile["fallbacks"]["developer"]=[{"engine":"claude","model":"fixture","effort":"high"}]
    selected,blocked=workers.select(db,rid,"developer",profile,engines)
    assert selected == profile["fallbacks"]["developer"][0] and blocked["engine"]=="codex"
    assert workers.select(db,rid,"reviewer",profile,engines)==({"engine":"antigravity"},None)


def install_fixture(tmp_path,monkeypatch):
    fake = Path(__file__).parent/"fixtures/quota_worker.py"
    bindir = tmp_path/"quota-bin"
    bindir.mkdir()
    if os.name == "nt":
        (bindir/"codex.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
    else:
        binary = bindir/"codex"
        binary.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fake))} "$@"\n')
        binary.chmod(0o700)
    monkeypatch.setenv("PATH",str(bindir)+os.pathsep+os.environ["PATH"])
    monkeypatch.setenv("OFFICE_FIXTURE_QUOTA",str(bindir))
    return bindir


def until(check,diagnostic):
    end = time.monotonic()+65
    while not check() and time.monotonic()<end:
        time.sleep(.1)
    assert check(), diagnostic()


def test_native_preflight_wait_restarts_only_declined_call_and_finishes_same_goal(goal,monkeypatch):
    from agent_office.goal_checks import _unprivileged
    runner,store,source,_ = goal
    db = store.connection
    repo = store.get_task(store.get_run(source).task_id).project
    receipt = db.execute("SELECT base_commit FROM studio_goals WHERE id=?",(source,)).fetchone()
    fixture = install_fixture(store.path.parent,monkeypatch)
    run_id = uuid4().hex
    runner.start(run_id,"room",repo,["."],"fixture change",True,reviewer_engine="codex",use_inbox=True,
        room_revision=receipt[0],room_assignment={"goal_policy":default_policy(["test_fixture.py"]),
        "goal_criteria":["fixture"],"work_profile":{"roles":{"developer":{"model":"fixture-model","effort":"medium"}}}})
    diag = lambda: [store.get_run(run_id).state,runner.detail(run_id)["diagnostic"],usage.describe(db,run_id)]
    if not _unprivileged():
        # The baseline runs repository pytest before any provider call. Service
        # and elevated runners must refuse it; regular-user installer CI exercises recovery.
        until(lambda:store.get_run(run_id).ended_at,diag)
        assert store.get_run(run_id).state == "FAILED"
        assert not usage.describe(db,run_id)["calls"] and not (fixture/"turns").exists()
        return
    until(lambda:goal_stage_wait.drained(db,run_id),diag)
    assert not (fixture/"turns").exists()
    assert goal_recovery.detail(store,run_id)["disposition"] == "waiting_quota"
    baseline = list(db.execute("SELECT receipt FROM studio_goal_checks WHERE run_id=? AND attempt=0",(run_id,)))
    assert baseline
    runner.close()
    with closing(LocalRunner(store.path)) as restarted:
        restarted._schedule(store)
        assert not (fixture/"turns").exists()
        (fixture/"grant").touch()
        # Move only the probe timer forward. The provider's actual answer still controls admission.
        db.execute("UPDATE studio_usage_holds SET next_probe_at=NULL WHERE run_id=?",(run_id,))
        until(lambda:store.get_run(run_id).ended_at, lambda: [restarted.detail(run_id),usage.describe(db,run_id)])
    assert store.get_run(run_id).state == "READY_FOR_HUMAN_REVIEW"
    assert (fixture/"turns").read_text().splitlines() == ["edit","review"]
    assert baseline == list(db.execute("SELECT receipt FROM studio_goal_checks WHERE run_id=? AND attempt=0",(run_id,)))
    report = usage.describe(db,run_id)
    assert report["total_tokens"] == 34 and report["hold"] is None
    assert [r["total_tokens"] for r in report["calls"]] == [0,17,17]
    assert report["last_resume_at"]

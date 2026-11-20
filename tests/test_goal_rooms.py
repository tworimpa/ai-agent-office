"""Goal settings, operator intent and candidate adoption through the workspace HTTP routes."""
from uuid import uuid4

from agent_office import goal_verification
from agent_office.store import TaskStore, encode
from agent_office.studio_ssh import _check_reply
from test_studio_http import project, request, studio
from test_workspace_rooms import engine_snapshot


def test_goal_intake_pins_policy_and_operator_adopts_the_reviewed_digest(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    database = tmp_path / "office.sqlite3"
    with studio(database) as server:
        monkeypatch.setattr(server.runner, "_launch", lambda *args: None)
        monkeypatch.setattr(server.engines, "snapshot", lambda: engine_snapshot("codex", "antigravity"))
        _, created = request(server, "/api/workspaces", {"name": "settings", "paths": [str(web.parent)]})
        workspace = created["workspace"]["id"]
        room = "/api/rooms/" + workspace
        status, body = request(server, room + "/goal-policy")
        assert status == 200
        policy = body["policy"]
        policy["max_attempts"] = 4
        assert request(server, room + "/goal-policy", policy) == (200, {"policy": policy})
        run_id = uuid4().hex
        payload = {"id": run_id, "message": "Windows 설정 유실을 재현하고 고쳐줘", "goal": True,
                   "allow_edits": True, "delegation": False, "meeting": True}
        status, receipt = request(server, room + "/assignments", payload)
        assert status == 202, receipt
        assert receipt["run"]["assignment"]["planning_mode"] == "direct"
        _check_reply(status, receipt, [server.journal_id.encode()], server.journal_id,
                     "POST", room[4:] + "/assignments", payload)
        journal = "/api/runs/" + run_id + "/goal"
        status, result = request(server, journal)
        assert status == 200, result
        goal = result["goal"]
        assert goal["request"] == payload["message"]
        assert len(goal["base_commit"]) == 40
        assert goal["scope"] == ["."] and goal["policy"] == policy
        assert goal["manual_checks"] == [payload["message"]]
        assert goal["candidates"] == [] and goal["best_attempt"] is None
        assert [event["kind"] for event in goal["events"]] == ["RUN_CREATED"]
        assert request(server, room + "/goal-policy", dict(policy, max_attempts=2))[0] == 200
        assert request(server, room + "/assignments", payload)[1]["run"]["assignment"]["goal_policy"] == policy
        with TaskStore(database, existing=True) as store, store._write():
            db = store.connection
            db.execute("UPDATE runs SET state='FAILED',outcome='TEST_FAILED',ended_at=started_at WHERE run_id=?", (run_id,))
            goal_verification.init_schema(db)
            for attempt in (1, 2):
                candidate = dict(attempt=attempt, digest=str(attempt)*64, eligible=True, score=attempt)
                patch = ("diff --git a/setting b/setting\nnew file mode 100644\n--- /dev/null\n+++ b/setting\n"
                         f"@@ -0,0 +1 @@\n+candidate {attempt}\n")
                db.execute("INSERT INTO studio_goal_candidates VALUES(?,?,?,?)", (run_id, attempt, encode(candidate), patch))
        first = dict(id=uuid4().hex, attempt=1, digest="1"*64, previous_digest=None)
        second = dict(id=uuid4().hex, attempt=2, digest="2"*64, previous_digest=first["digest"])
        assert "+candidate 1" in request(server, journal + "/candidates/1")[1]["diff"]
        status, comparison = request(server, journal + "/compare/1/2")
        assert status == 200 and comparison["format"] == "files", comparison
        assert "-candidate 1\n+candidate 2" in comparison["diff"]
        assert request(server, journal + "/adopt", first)[0] == 200
        assert request(server, journal + "/adopt", second)[0] == 200
        assert request(server, journal + "/adopt", first)[0] == 200
        assert request(server, journal + "/adopt", dict(first, id=uuid4().hex))[0] == 409
        final = request(server, journal)[1]["goal"]
        assert final["adopted_attempt"] == 2 and final["merge_status"] == final["deployment_status"] == "not_recorded"
        assert [e["attempt"] for e in final["events"] if e["kind"] == "adopted"] == [1, 2]

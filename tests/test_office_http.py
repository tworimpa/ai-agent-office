"""Read-only local/peer Studio office, pinned identities and real waiting fixture workers."""
from io import BytesIO
import json
import time
import uuid

import pytest

from agent_office.office_state import PAGE_SIZE
from agent_office.studio_ssh import RemoteHost, RemoteUnavailable, _read_response, is_peer_route
from test_inbox_http import install_inbox_fixture
from test_remote_studio import LoopbackLink
from test_studio_http import project, request, studio, wait_for
from test_studio_ssh import frame


def until(get, predicate):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        data = get()
        if predicate(data):
            return data
        time.sleep(.12)
    pytest.fail(f"Office state timed out: {data}")


def test_office_page_assets_navigation_and_query_contract(tmp_path):
    with studio(tmp_path / "journal.db") as office:
        for path in ("/office", "/office?host=local", "/office?host=" + "a"*32):
            code, page = request(office, path)
            assert code == 200 and "오피스 보기" in page.decode()
        for query in ("host=", "run=123", "host=local&host=local", "host=unknown", "host=local&run=" + "a"*32):
            assert request(office, "/office?" + query)[0] == 400
        assert b"/office" in request(office, "/")[1]
        for path in ("/office.css", "/office.js"):
            assert request(office, path)[0] == 200
        code, portrait = request(office, "/office-agents.webp")
        assert code == 200 and portrait[:4] == b"RIFF" and portrait[8:12] == b"WEBP"
        code, page = request(office, "/api/office")
        assert code == 200 and page["agents"] == [] and page["total_runs"] == 0
        assert page["counts"] == {"active_runs": 0, "questions": 0, "permissions": 0}
        assert request(office, "/api/office", {})[0] in (404, 405)
        assert request(office, "/api/office/page/1")[0] in (400, 404)
        assert request(office, "/api/office/page/24")[1]["agents"] == []


def test_remote_office_tracks_parallel_children_and_the_exact_waiting_role(tmp_path, monkeypatch):
    trace = install_inbox_fixture(tmp_path, monkeypatch)
    web, contract = project(tmp_path)
    with studio(tmp_path / "peer.db", accept_remote=True) as peer:
        host = RemoteHost("builder", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)

        def relay(target, method, route, body):
            status, reply = link(target, method, route, body)
            raw = BytesIO(frame(json.dumps(reply).encode(), status, peer.journal_id))
            return _read_response(raw, host.journal_id, method, route, body)

        monkeypatch.setattr("agent_office.studio_server.remote_request", relay)
        with studio(tmp_path / "local.db", remotes=[host]) as local:
            api = "/api/hosts/" + host.journal_id
            status, ws = request(local, api + "/workspaces", {"name": "서비스", "paths": [str(web), str(contract)]})
            assert status == 201
            group_id = uuid.uuid4().hex
            job = {"id": group_id, "workspace_id": ws["workspace"]["id"], "task": "주문 규격 확인",
                   "indices": [0, 1], "developer_engine": "cline", "allow_edits": False, "use_inbox": True,
                   "subtasks": [{"task": "화면 조사", "indices": [0]}, {"task": "규격 조사", "indices": [1]}]}
            status, receipt = request(local, api + "/runs", job)
            assert status == 202
            ids = {child["id"] for child in receipt["run"]["subtasks"]}

            def snapshot():
                status, data = request(local, api + "/office")
                assert status == 200, data
                return data

            waiting = until(snapshot, lambda doc: doc["counts"]["questions"] == 2)
            assert waiting["counts"] == {"active_runs": 2, "questions": 2, "permissions": 0}
            assert {a["run_id"] for a in waiting["agents"]} == ids
            assert all(a["role"] == "developer" and a["engine"] == "cline" and a["activity"] == "waiting"
                       and a["parent_id"] == group_id and a["parent_task"] == job["task"] for a in waiting["agents"])
            assert request(local, "/api/office")[1]["total_runs"] == 0
            target = waiting["agents"][0]
            child_id = target["run_id"]
            base = f"{api}/runs/{child_id}"
            assert request(local, f"/?host={host.journal_id}&run={child_id}")[0] == 200
            assert request(local, base)[1]["run"]["parent_id"] == group_id
            q = target["requests"][0]
            assert q["kind"] == "question" and q["prompt"] == "어느 규격을 적용할까요?"
            assert request(local, base + "/inbox/" + q["id"] + "/decision",
                           {"action": "answer", "answer": "use spec v2"})[0] == 200
            both = until(snapshot, lambda doc: doc["counts"]["permissions"] == 1)
            assert both["counts"]["questions"] == 1
            perm = next(a for a in both["agents"] if a["run_id"] == child_id)["requests"][0]
            assert perm["kind"] == "permission" and not trace.exists()
            link.online = False
            assert request(local, api + "/office")[0] == 503
            assert request(local, "/api/office")[0] == 200
            link.online = True
            assert next(a for a in snapshot()["agents"] if a["run_id"] == child_id)["requests"][0]["id"] == perm["id"]
            assert request(local, base + "/inbox/" + perm["id"] + "/decision", {"action": "reject"})[0] == 200
            wait_for(local, child_id, {"PLAN_ONLY"}, api=api)
            after = snapshot()
            finished = next(a for a in after["agents"] if a["run_id"] == child_id)
            assert finished["activity"] == "done" and finished["request_count"] == 0
            assert after["counts"]["permissions"] == 0
            assert request(local, api + "/runs/" + group_id + "/stop", {})[0] == 200
            assert all(method == "GET" for method, route in link.calls if route.startswith("/office"))


def test_remote_office_frame_is_bound_to_peer_page_and_assignment():
    pin, rid, qid = (uuid.uuid4().hex for _ in range(3))
    stamp = "2026-11-19T01:00:00+00:00"
    row = dict(run_id=rid, role="developer", engine="cline", task="작업", parent_id=None, parent_task=None,
               activity="waiting", run_state="RUNNING", request_count=1, updated_at=stamp,
               requests=[dict(id=qid, kind="question", prompt="답이 필요합니다")])
    data = dict(generated_at=stamp, total_runs=1, offset=0, next_offset=None,
                counts=dict(active_runs=1, questions=1, permissions=0), agents=[row])

    def read(body, journal=pin, route="/office"):
        return _read_response(BytesIO(frame(json.dumps(body).encode(), journal=journal)), pin, "GET", route, None)

    assert read(data) == (200, data)
    assert not is_peer_route("POST", "/office") and is_peer_route("GET", f"/office/page/{PAGE_SIZE}")
    with pytest.raises(RemoteUnavailable):
        read(data, uuid.uuid4().hex)
    with pytest.raises(RemoteUnavailable):
        read(data, route=f"/office/page/{PAGE_SIZE}")
    for bad in ([dict(row, run_id="outside")], [row, row], [dict(row, requests=[])],
                [dict(row, parent_id=rid)], [dict(row, activity="done")], [dict(row, engine="unknown")],
                [dict(row, run_state="REVIEWING")]):
        with pytest.raises(RemoteUnavailable):
            read(dict(data, agents=bad))
    for counts in ({"active_runs": 0, "questions": 1, "permissions": 0},
                   {"active_runs": 1, "questions": 2, "permissions": 0}):
        with pytest.raises(RemoteUnavailable):
            read(dict(data, counts=counts))

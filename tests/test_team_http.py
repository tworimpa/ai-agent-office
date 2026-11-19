"""Check host selection, replay and parent-visible partial validation over Studio's wire contract."""
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import sys
import time
import uuid

from agent_office.studio_ssh import RemoteHost, _read_response
from test_remote_studio import LoopbackLink
from test_studio_http import request, studio
from test_studio_ssh import frame
from test_team_runs import git


def install_workers(tmp_path, monkeypatch):
    gate = tmp_path / "gate"
    gate.mkdir()
    monkeypatch.setenv("OFFICE_TEAM_GATE", str(gate))
    bin_dir = tmp_path / "test cli"
    bin_dir.mkdir()
    script = Path(__file__).parent / "helpers" / "team_worker.py"
    for name in ("codex", "claude", "agy"):
        if os.name == "nt":
            (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" {name} %*\n')
        else:
            wrapper = bin_dir / name
            wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {name} "$@"\n')
            wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return gate


def project(tmp_path):
    repo = tmp_path / "원격 작업"
    repo.mkdir()
    git(repo, "init", "-q")
    for name in ("web", "api"):
        (repo / name).mkdir()
        (repo / name / "value.txt").write_text("base")
    (repo / "test_independent.py").write_text(
        "from pathlib import Path\n"
        "def test_one_verified_change():\n"
        "    values = [p.read_text() for p in Path(__file__).parent.glob('*/value.txt')]\n"
        "    assert sorted(values) == ['base', 'ready']\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    return repo


def until(get, valid, timeout=50):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = get()
        if valid(value):
            return value
        time.sleep(.1)
    raise AssertionError(f"Fixture timed out: {value}")


def test_remote_parent_preserves_individual_skills_changes_and_test_failures(tmp_path, monkeypatch):
    gate = install_workers(tmp_path, monkeypatch)
    repo = project(tmp_path)
    revision = git(repo, "rev-parse", "HEAD")
    peer_db, local_db = tmp_path / "peer.db", tmp_path / "controller.db"
    with studio(peer_db, accept_remote=True) as peer:
        host = RemoteHost("builder", peer.server_port, peer.journal_id)
        link = LoopbackLink(peer, host)

        def relay(target, method, route, payload):
            status, body = link(target, method, route, payload)
            wire = BytesIO(frame(json.dumps(body).encode(), status, peer.journal_id))
            return _read_response(wire, host.journal_id, method, route, payload)

        monkeypatch.setattr("agent_office.studio_server.remote_request", relay)
        api = "/api/hosts/" + host.journal_id
        with studio(local_db, remotes=[host]) as local:
            def register(name, text, prefix=api):
                status, data = request(local, prefix + "/skills", {"name": name, "instructions": text})
                assert status == 201, data
                return data["skill"]

            dev = register("구현", "IMPLEMENT_ONE_PART")
            rev = register("검토", "CHECK_THE_RESULT")
            foreign = register("구현", "IMPLEMENT_ONE_PART", "/api")
            assert foreign["id"] != dev["id"]
            listing = request(local, api + "/skills")[1]["skills"]
            assert {s["id"] for s in listing} == {dev["id"], rev["id"]}
            assert all("instructions" not in s for s in listing)
            assert request(local, api + "/skills/" + dev["id"])[1]["skill"]["instructions"] == "IMPLEMENT_ONE_PART"

            status, data = request(local, api + "/workspaces", {"name": "팀", "paths": [str(repo / "web"), str(repo / "api")]})
            assert status == 201
            parent = uuid.uuid4().hex
            plan = [{"task": "web-goal", "indices": [0], "developer_engine": "claude"},
                    {"task": "api-goal", "indices": [1], "developer_engine": "codex"}]
            skills = {"developer": [dev["id"]], "reviewer": [rev["id"]]}
            job = {"id": parent, "workspace_id": data["workspace"]["id"], "indices": [0, 1], "task": "Keep the original checkout clean",
                   "allow_edits": True, "subtasks": plan, "role_skills": skills}
            assert request(local, api + "/runs", {**job, "role_skills": {"developer": [foreign["id"]]}})[0] == 400
            assert request(local, api + "/runs")[1]["runs"] == []
            link.drop_receipt = True
            assert request(local, api + "/runs", job)[0] == 503
            link.online = True
            status, data = request(local, api + "/runs", job)
            assert status == 202, data
            group = data["run"]
            assert group["base_revision"] == revision and group["role_skills"] == skills
            ids = [s["id"] for s in group["subtasks"]]
            assert len(set(ids)) == 2
            assert request(local, api + "/runs", {**job, "subtasks": list(reversed(plan))})[0] == 409
            until(lambda: len(list(gate.glob("*.started"))), lambda n: n == 2)
            newer = register("구현", "REPLACEMENT_INSTRUCTION")
            assert newer["version"] == dev["version"] + 1
            assert register("구현", "IMPLEMENT_ONE_PART") == dev
            assert request(local, "/api/runs")[1]["runs"] == []
            (gate / "continue").touch()

        with studio(local_db, remotes=[host]) as reopened:
            def detail(rid):
                status, data = request(reopened, api + "/runs/" + rid)
                assert status == 200, data
                return data["run"]

            group = until(lambda: detail(parent), lambda g: g["state"] == "FAILED")
            assert group["outcome"] == "TEST_FAILED"
            assert [child["id"] for child in group["subtasks"]] == ids
            assert [child["state"] for child in group["subtasks"]] == ["READY_FOR_HUMAN_REVIEW", "FAILED"]
            ok, failed = [detail(rid) for rid in ids]
            for child in (ok, failed):
                assert child["kind"] == "subtask" and child["parent_id"] == parent and child["base_revision"] == revision
                assert child["role_skill_snapshots"]["developer"] == [dev]
                assert git(Path(child["workspace"]), "rev-parse", "HEAD") == revision
            assert "1 passed" in ok["test_output"] and "Reviewed web-goal" in ok["review_output"] and "+ready" in ok["diff"]
            assert "1 failed" in failed["test_output"] and not failed["review_output"] and "+needs-fix" in failed["diff"]
            assert ok["workspace"] != failed["workspace"] and (repo / "web/value.txt").read_text() == "base"
            assert git(repo, "status", "--porcelain") == ""
            assert request(reopened, api + "/runs")[1]["runs"][0]["id"] == parent
            assert len(request(reopened, api + "/runs")[1]["runs"]) == 1

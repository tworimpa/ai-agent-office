from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import sqlite3
import subprocess
import time
from uuid import uuid4
import pytest

from agent_office import goal_checks, goal_peer, studio_ssh
from agent_office.store import TaskStore
from test_studio_http import studio


def test_alias_matching_owner_hostname_requires_an_explicit_peer_journal(monkeypatch):
    with sqlite3.connect(":memory:") as db:
        goal_peer.ensure_schema(db)
        peer = uuid4().hex
        db.execute("INSERT INTO studio_goal_peers VALUES(?, 'host-a', 8766)", (peer,))
        monkeypatch.setattr(goal_peer.platform, "node", lambda: "host-a")
        goal = uuid4().hex
        with pytest.raises(ValueError, match="저널 ID"):
            goal_peer.pin_targets(db, goal, {"checks":[{"host":"host-a"}]})
        goal_peer.pin_targets(db, goal, {"checks":[{"host":peer}]})
        assert db.execute("SELECT journal_id FROM studio_goal_peer_targets").fetchone()[0] == peer


@pytest.mark.parametrize("edit", ["skipped","counts","stage","recipe","argv","host","environment","started","finished","order"])
def test_peer_receipt_rejects_unbound_or_incomplete_verification(edit):
    identifier, ticket = uuid4().hex, uuid4().hex
    profile = {"stage":"full","argv":["pytest","-q"],"timeout_seconds":20}
    requested = {"base":"1"*40,"source":"a"*64,"recipe":goal_peer._selector(profile),"limit_ms":20000}
    result = {"status":"passed", "exit_code":0,"counts":{"passed":1}, "source_digest":requested["source"],
              "source_commit":"2"*40, "stage":"full", "recipe_argv":profile["argv"],"timeout_seconds":20,
              "environment":{"python":"3.13","host":"peer","platform":"Linux","digest":"3"*64},
              "host":"peer","cwd":"/tmp/candidate","argv":["/bin/python","-I","-B","/app/goal_pytest.py","-q",
                                                            "-o","junit_family=xunit1","-o","cache_dir=/tmp/cache",
                                                            "--junitxml","/tmp/report.xml"],
              "elapsed_seconds":1,"started_at":"2026-11-20T13:00:00+00:00",
              "finished_at":"2026-11-20T13:00:01+00:00","summary":""}
    packet = dict(requested,id=identifier,ticket=ticket,commit="2"*40,state="finished",result=result,
                  cases={"test_one":"passed"})
    goal_peer.check_reply(200,packet,identifier,requested)
    if edit=="skipped":
        packet["cases"]["test_one"]="skipped"
        result["counts"]={"skipped":1}
    elif edit=="counts":
        result["counts"]={"passed":True}
    elif edit=="stage":
        result["stage"]="quick"
    elif edit=="recipe":
        result["recipe_argv"]=["pytest","-q","test_other.py"]
    elif edit=="argv":
        result["argv"]=["/bin/python","--version"]
    elif edit=="host":
        result["host"]="different-peer"
    elif edit=="environment":
        result["environment"]["digest"]=""
    elif edit=="started":
        result["started_at"]="2026-11-20T13:00:00"
    elif edit=="finished":
        result["finished_at"]="invalid"
    else:
        result["finished_at"]="2026-11-20T12:00:00+00:00"
    with pytest.raises(ValueError):
        goal_peer.check_reply(200,packet,identifier,requested)


def test_manifest_uses_git_text_conversion_without_normalizing_binary(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()
    git("init", "-q")
    git("config", "core.autocrlf", "true")
    (tmp_path/".gitattributes").write_bytes(b"*.bin -text\n*.txt text eol=crlf\n")
    (tmp_path/"tracked.txt").write_bytes(b"first\nsecond\n")
    (tmp_path/"default.py").write_bytes(b"x = 1\ny = 2\n")
    # The -text attribute protects even binary records containing only printable bytes.
    (tmp_path/"record.bin").write_bytes(b"first\r\nsecond\r\n")
    git("add", ".")
    git("-c","user.name=Test","-c","user.email=a@example.invalid","commit","-qm","text and binary")
    commit = git("rev-parse","HEAD")
    approved = goal_peer._manifest(goal_peer.committed_files(tmp_path/".git",commit))
    for name in ("default.py","tracked.txt"):
        file = tmp_path/name
        file.write_bytes(file.read_bytes().replace(b"\n",b"\r\n"))
    assert goal_peer.source_digest(tmp_path)==approved
    (tmp_path/"record.bin").write_bytes(b"first\nsecond\n")
    assert goal_peer.source_digest(tmp_path)!=approved


def test_two_studios_match_an_operator_armed_commit_and_recover_lost_ack(tmp_path, monkeypatch):
    source, remote_repo = tmp_path / "source", tmp_path / "peer-source"
    source.mkdir()
    def git(root, *args):
        return subprocess.check_output(["git", *args], cwd=root).decode().strip()
    git(source, "init", "-q")
    git(source, "config", "core.autocrlf", "false")
    test = source / "test_remote.py"
    test.write_bytes(b"def test_remote(): assert False\n")
    git(source, "add", ".")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git(source, "rev-parse", "HEAD")
    git(source, "-c", "core.autocrlf=false", "clone","-q",str(source),str(remote_repo))
    git(remote_repo, "config", "core.autocrlf", "false")
    test.write_bytes(b"def test_remote(): assert True\n")
    (remote_repo / test.name).write_bytes(test.read_bytes())
    git(remote_repo, "add", ".")
    git(remote_repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "approved candidate")
    commit = git(remote_repo, "rev-parse", "HEAD")
    wire = []
    def channel(host, method, route, payload=None):
        wire.append(method)
        frame = studio_ssh._request_bytes(host, method, route, payload)
        with socket.create_connection(("127.0.0.1",host.port), timeout=8) as client:
            client.sendall(frame)
            with client.makefile("rb") as stream:
                return studio_ssh._read_response(stream, host.journal_id, method, route, payload)
    peer_db, origin_db = tmp_path / "peer.db", tmp_path / "origin.db"
    with studio(peer_db,accept_remote=True) as peer:
        host = studio_ssh.RemoteHost("linux-check",peer.server_address[1],peer.journal_id)
        with studio(origin_db,remotes=[host]), ThreadPoolExecutor(max_workers=1) as pool, TaskStore(origin_db) as owner:
            workspace = peer.catalog.create("trusted source", [str(remote_repo)])["id"]
            check = {"stage":"full","host":"local","argv":["python","-m","pytest","-q"],"timeout_seconds":20}
            sender = dict(check, host=host.alias)
            accepted = pool.submit(goal_peer.prepare,peer_db,workspace,base,commit,check,15)
            with TaskStore(peer_db) as store:
                for _ in range(100):
                    if store.connection.execute("SELECT 1 FROM studio_peer_checks WHERE state='offered'").fetchone():
                        break
                    time.sleep(.03)
                else:
                    accepted.result(timeout=1)
                    raise AssertionError("local offer missing")
            expected = {"base":base,"source":goal_peer.source_digest(source),
                        "recipe":goal_peer._selector(check),"limit_ms":20000}
            assert channel(host,"POST","/goal-checks/"+uuid4().hex,dict(expected,source="c"*64))[0] == 404
            wire.clear()
            def lose_ack(h, method, route, payload=None):
                reply = channel(h, method, route, payload)
                if method == "POST":
                    raise studio_ssh.RemoteUnavailable()
                return reply
            monkeypatch.setattr(studio_ssh,"remote_request",lose_ack)
            goal_id = uuid4().hex
            goal_peer.pin_targets(owner.connection,goal_id,{"checks":[sender]})
            receipt, cases = goal_peer.run_remote(owner,goal_id,1,1,source,base,sender,
                                                   timeout=25,on_start=lambda receipt: None)
            assert wire.count("POST") == 1 and "GET" in wire
            if goal_checks._unprivileged():
                assert accepted.result(timeout=5)
                assert receipt["status"] == "passed" and list(cases.values()) == ["passed"]
                assert receipt["source_commit"] == commit and receipt["host"]
            else:
                assert not accepted.result(timeout=5) and receipt["status"]=="unverified"
            assert receipt["source_digest"]==expected["source"] and receipt["peer_journal"]==peer.journal_id
            route = "/goal-checks/"+receipt["peer_request"]
            saved = channel(host,"GET",route)
            assert saved[0] == 200 and saved[1]["state"] == "finished"
            assert channel(host,"POST",route,expected) == saved
            assert channel(host,"POST",route,dict(expected,base="d"*40))[0] == 409
            assert channel(host,"POST","/goal-checks/"+uuid4().hex,expected)[0] == 404

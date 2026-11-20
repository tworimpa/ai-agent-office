"""One-use verification offers, armed by a foreground operator on the execution host."""
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import re
import stat
import sys
from tempfile import TemporaryDirectory
import time
import uuid

from .goal_checks import recipe, run_check
from .goal_candidates import private_name
from .meeting_parallel import _check_destination, _contents
from .models import OfficeError, validate_id
from .ownership import RunGuard
from .release_store import run_git
from .release_validation import MAX_FILES, MAX_SOURCE, committed_files
from .store import TaskStore, encode, now
from .studio_catalog import StudioCatalog, _git


def _digest(data):
    return hashlib.sha256(encode(data).encode()).hexdigest()


def _safe_names(names):
    if len(names) > MAX_FILES or any(private_name(name) for name in names):
        raise ValueError("비공개 파일 또는 크기 제한 때문에 원격 검증 소스를 고정할 수 없습니다.")


def _manifest(files):
    return _digest(sorted((name, mode, hashlib.sha256(content).hexdigest()) for name, mode, content in files))


def _runtime_digest(root, check):
    from .goal_recovery import _environment
    digest = hashlib.sha256()
    _environment(root,[check],digest.update)
    return digest.hexdigest()


def source_digest(root):
    """Compare canonical Git text and literal binary bytes with the approved commit."""
    index = {}
    for row in run_git(root, "ls-files", "--stage", "-z").split(b"\0")[:-1]:
        header, path = row.split(b"\t", 1)
        mode, _, stage = header.split()
        if mode not in (b"100644", b"100755") or stage != b"0":
            raise ValueError("병합 충돌, 링크 또는 하위 모듈이 포함된 원격 검증 소스입니다.")
        index[path.decode()] = int(mode, 8) & 0o777
    new = run_git(root, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")[:-1]
    names = sorted(index.keys() | set(new))
    _safe_names(names)
    _check_destination(root, names)
    autocrlf = _git(root, "config", "--default=false", "--get", "core.autocrlf",
                    error="줄바꿈 변환 설정을 확인할 수 없습니다.").lower() in {"true","input","yes","on","1"}
    convert = set()
    for entry in run_git(root,"ls-files","--eol","--cached","--others","--exclude-standard","-z").split(b"\0")[:-1]:
        columns, name = entry.split(b"\t", 1)
        indexed, working, attr = columns.strip().split(None, 2)
        # Never translate binary, -text, mixed, or stored CRLF files. Git has already
        # classified the content. No clean filter or repository-provided command is run.
        if (indexed in (b"i/lf",b"i/none",b"i/") and working == b"w/crlf"
                and (attr.startswith(b"attr/text") or attr == b"attr/" and autocrlf)):
            convert.add(name.decode())
    files, total = [], 0
    for name in names:
        path = root / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        total += info.st_size
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or total > MAX_SOURCE:
            raise ValueError("원격 검증 소스의 파일 종류나 크기를 확인하세요.")
        with path.open("rb") as stream:
            if not os.path.samestat(info, os.fstat(stream.fileno())):
                raise ValueError("검증 소스가 수집 중 변경되었습니다.")
            content = stream.read(info.st_size + 1)
        if len(content) != info.st_size:
            raise ValueError("검증 소스가 수집 중 변경되었습니다.")
        if name in convert and b"\0" not in content:
            content = content.replace(b"\r\n", b"\n")
        mode = (index.get(name, 0o644) if os.name == "nt" else
                0o755 if info.st_mode & stat.S_IXUSR else 0o644)
        files.append((name, mode, content))
    return _manifest(files)


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_peers(
        journal_id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE, port INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_peer_targets(
        run_id TEXT NOT NULL, requested TEXT NOT NULL, journal_id TEXT NOT NULL, alias TEXT NOT NULL,
        port INTEGER NOT NULL, PRIMARY KEY(run_id,requested))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_peer_checks(
        ticket TEXT PRIMARY KEY, base TEXT NOT NULL, source TEXT NOT NULL, recipe TEXT NOT NULL, sha TEXT NOT NULL,
        expires REAL NOT NULL, request_id TEXT UNIQUE, state TEXT NOT NULL, result TEXT, cases TEXT,
        limit_ms INTEGER, deadline REAL)""")


def configure(path, peers):
    with TaskStore(path, existing=True) as store, store._write():
        db = store.connection
        ensure_schema(db)
        db.execute("DELETE FROM studio_goal_peers")
        db.executemany("INSERT INTO studio_goal_peers VALUES(?,?,?)", [(h.journal_id,h.alias,h.port) for h in peers])


def pin_targets(db, run_id, policy):
    ensure_schema(db)
    for requested in {c["host"] for c in policy.get("checks", [])}:
        if requested.casefold() == "local":
            continue
        peers = list(db.execute("SELECT journal_id,alias,port FROM studio_goal_peers WHERE journal_id=? "
                                "OR alias=? COLLATE NOCASE", (requested,requested)))
        if requested.casefold() == platform.node().casefold():
            if peers:
                raise ValueError("현재 컴퓨터 이름과 SSH 별칭이 같습니다. 원격 저널 ID 또는 local을 지정하세요.")
            continue
        if len(peers) != 1:
            raise ValueError("검증할 SSH 호스트의 등록된 저널 ID 또는 고유 별칭을 확인하세요.")
        db.execute("INSERT INTO studio_goal_peer_targets VALUES(?,?,?,?,?)",(run_id,requested,*peers[0]))


def _selector(check):
    return _digest([check["stage"], check["argv"], check["timeout_seconds"]])


def _validate(payload):
    if (not isinstance(payload, dict) or set(payload) != {"base","source","recipe","limit_ms"} or
            not all(isinstance(payload[k],str) for k in ("base","source","recipe")) or
            type(payload["limit_ms"]) is not int or not 1 <= payload["limit_ms"] <= 86400000 or
            not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", payload["base"]) or
            not all(re.fullmatch(r"[0-9a-f]{64}", payload[k]) for k in ("source","recipe"))):
        raise ValueError("원격 검증에는 고정된 소스와 검증 정의 식별자가 필요합니다.")


def lookup(path, identifier, payload=None):
    """A peer can claim an existing local offer. This handler never starts a process."""
    validate_id(identifier)
    if payload is not None:
        _validate(payload)
    with TaskStore(path, existing=True) as store, store._write():
        db = store.connection
        row = db.execute("SELECT * FROM studio_peer_checks WHERE request_id=?", (identifier,)).fetchone()
        if row and payload is not None and any(row[k] != v for k,v in payload.items()):
            raise OfficeError("REQUEST_CONFLICT")
        if not row and payload is not None:
            for entry in db.execute("SELECT * FROM studio_peer_checks WHERE state='offered' AND expires>? "
                                    "AND base=? AND source=? AND recipe=? ORDER BY rowid",
                                    (time.time(), payload["base"], payload["source"], payload["recipe"])):
                if RunGuard(path, entry["ticket"]).is_held():
                    db.execute("UPDATE studio_peer_checks SET request_id=?,state='claimed',limit_ms=?,deadline=? "
                               "WHERE ticket=?", (identifier,payload["limit_ms"],time.time()+payload["limit_ms"]/1000,
                                                  entry["ticket"]))
                    row = db.execute("SELECT * FROM studio_peer_checks WHERE request_id=?", (identifier,)).fetchone()
                    break
        if not row:
            raise OfficeError("NOT_FOUND")
        state = row["state"]
        if state in ("claimed","running") and not RunGuard(path, row["ticket"]).is_held():
            db.execute("UPDATE studio_peer_checks SET state='interrupted' WHERE ticket=?", (row["ticket"],))
            state = "interrupted"
        return {"id":identifier, "ticket":row["ticket"], "base":row["base"], "source":row["source"],
                "recipe":row["recipe"], "limit_ms":row["limit_ms"], "commit":row["sha"], "state":state,
                "result":json.loads(row["result"]) if row["result"] else None,
                "cases":json.loads(row["cases"]) if row["cases"] else {}}


def _check_ids(base, commit):
    if not all(isinstance(v,str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}",v) for v in (base,commit)):
        raise ValueError("기준 및 검증 커밋의 전체 SHA가 필요합니다.")


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("원격 검증 시각이 누락되었습니다.")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("원격 검증 시각의 시간대가 누락되었습니다.")
    return parsed


def _invocation_matches(result):
    from .goal_checks import _pytest_files
    argv, planned = result["argv"], result["recipe_argv"]
    kind = recipe(planned)
    if kind == "pytest":
        if len(argv) < 11 or argv[1:3] != ["-I","-B"] or PureWindowsPath(argv[3]).name != "goal_pytest.py":
            return False
        if (argv[4:8] != ["-q","-o","junit_family=xunit1","-o"] or not argv[8].startswith("cache_dir=")
                or argv[9] != "--junitxml"):
            return False
        paths = _pytest_files(planned)
        expected = (["-x"] if "-x" in planned else []) + (["--",*paths] if paths else [])
        return argv[11:] == expected
    if PureWindowsPath(argv[0]).name.casefold() not in ("node","node.exe"):
        return False
    if kind == "web":
        index = 3 if argv[1:2] == ["--import"] else 1
        if (argv[index:index+2] != ["--test","--test-reporter=junit"] or len(argv)<=index+3
                or not argv[index+2].startswith("--test-reporter-destination=")):
            return False
        return all(re.fullmatch(r"tests/web/[^/\\]+\.test\.mjs",path) for path in argv[index+3:])
    artifact = result.get("artifact")
    return (len(argv)==3 and isinstance(artifact,dict) and artifact.get("executable")==argv[2]
            and all(isinstance(artifact.get(k),str) and re.fullmatch(r"[a-f0-9]{64}",artifact[k])
                    for k in ("executable_sha256","archive_sha256","source_sha256")))


def check_reply(status, data, identifier, requested):
    if status != 200 or data.get("id") != identifier:
        raise ValueError("원격 요청 ID를 확인하세요.")
    validate_id(data.get("ticket"))
    _check_ids(data.get("base"), data.get("commit"))
    bound = {key:data.get(key) for key in ("base","source","recipe","limit_ms")}
    _validate(bound)
    if requested is not None and requested != bound:
        raise ValueError("다른 검증에 대한 응답입니다.")
    state, result, cases = data.get("state"), data.get("result"), data.get("cases")
    if state in ("claimed","running","interrupted") and result is None and cases == {}:
        return
    if state != "finished" or not isinstance(result,dict) or not isinstance(cases,dict) or len(cases)>20000:
        raise ValueError("원격 검증이 종료되지 않았습니다.")
    profile = {"stage":result.get("stage"), "argv":result.get("recipe_argv"), "timeout_seconds":result.get("timeout_seconds")}
    if (profile["stage"] not in ("quick","full","journey") or type(profile["timeout_seconds"]) is not int
            or not 1 <= profile["timeout_seconds"] <= 86400 or recipe(profile["argv"]) == "host"
            or _selector(profile) != bound["recipe"] or result.get("source_digest") != bound["source"]
            or result.get("source_commit") != data["commit"]):
        raise ValueError("원격 검증 소스와 실행 정의가 요청과 다릅니다.")
    env, host, cwd, argv = (result.get(k) for k in ("environment","host","cwd","argv"))
    if (not isinstance(env,dict) or not isinstance(host,str) or not host.strip() or len(host)>253
            or host!=env.get("host") or not isinstance(env.get("digest"),str)
            or not re.fullmatch(r"[a-f0-9]{64}",env["digest"])
            or not all(isinstance(env.get(k),str) and env[k].strip() for k in ("python","platform"))
            or not isinstance(cwd,str) or not (PurePosixPath(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute())
            or not isinstance(argv,list) or not 1<=len(argv)<=2048 or not all(isinstance(s,str) and s for s in argv)):
        raise ValueError("원격 검증의 호스트 또는 실행 환경을 확인할 수 없습니다.")
    if (_timestamp(result.get("finished_at")) < _timestamp(result.get("started_at"))
            or type(result.get("elapsed_seconds")) not in (float,int) or not 0<=result["elapsed_seconds"]<=172800
            or not isinstance(result.get("summary"),str) or len(result["summary"])>8192
            or not all(isinstance(k,str) and 0<len(k)<=2048 and v in ("passed","skipped","failed") for k,v in cases.items())
            or not isinstance(result.get("counts"),dict) or any(type(n) is not int for n in result["counts"].values())
            or result["counts"] != dict(Counter(cases.values())) or result.get("status") not in ("passed","failed","timeout","unverified")
            or result.get("exit_code") is not None and type(result["exit_code"]) is not int
            or result["status"] in ("passed","failed") and (result.get("exit_code") is None or not _invocation_matches(result))
            or result["status"]=="passed" and (not result["counts"].get("passed") or "failed" in result["counts"]
                                                or result.get("exit_code") != 0)):
        raise ValueError("원격 검증의 실제 실행 결과를 확인할 수 없습니다.")


def prepare(path, workspace_id, base, commit, check, wait_seconds):
    """The local operator supplies the source, executable recipe and time limit."""
    _check_ids(base, commit)
    if (check["stage"] not in ("quick","full","journey") or not 1 <= check["timeout_seconds"] <= 86400
            or not 1 <= wait_seconds <= 604800 or "--lf" in check["argv"]):
        raise ValueError("실제로 실행할 검증 범위와 시간을 지정하세요.")
    if recipe(check["argv"]) == "host":
        raise ValueError("프로젝트 원본에 연결된 사용자 정의 명령은 해당 호스트의 로컬 목표에서 실행하세요.")
    repo, folders = StudioCatalog(path).select(workspace_id,[0])
    if folders != ["."]:
        raise ValueError("검증용 체크아웃의 저장소 루트를 첫 폴더로 등록하세요.")
    run_git(repo, "merge-base", "--is-ancestor", base, commit)
    _safe_names(run_git(repo,"ls-tree","-rz","--name-only",commit).decode().split("\0")[:-1])
    gitdir = Path(run_git(repo,"rev-parse","--absolute-git-dir").decode().strip())
    files = committed_files(gitdir, commit)
    ticket, digest = uuid.uuid4().hex, _manifest(files)
    with TaskStore(path, existing=True) as store, RunGuard(path,ticket):
        db = store.connection
        ensure_schema(db)
        expiry = time.time() + wait_seconds
        db.execute("INSERT INTO studio_peer_checks(ticket,base,source,recipe,sha,expires,state) "
                   "VALUES(?,?,?,?,?,?,'offered')",
                   (ticket,base,digest,_selector(check),commit,expiry))
        print(encode({"ticket":ticket, "source":digest, "expires":expiry}), flush=True)
        try:
            while True:
                row = db.execute("SELECT state,deadline FROM studio_peer_checks WHERE ticket=?", (ticket,)).fetchone()
                if row[0] == "claimed":
                    break
                if time.time() >= expiry:
                    if db.execute("UPDATE studio_peer_checks SET state='expired' WHERE ticket=? AND state='offered'",
                                  (ticket,)).rowcount:
                        return False
                time.sleep(.15)
            db.execute("UPDATE studio_peer_checks SET state='running' WHERE ticket=?", (ticket,))
            with TemporaryDirectory(prefix="office-peer-") as directory:
                root = Path(directory)
                for name, mode, content in files:
                    dest = root / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(content)
                    dest.chmod(mode)
                remaining = min(check["timeout_seconds"], row["deadline"] - time.time())
                if remaining <= 0:
                    return False
                environment = _runtime_digest(root, check)
                result, cases = run_check(root, check, timeout=remaining)
                modes = {name: mode for name, mode, _ in files}
                try:
                    actual = _contents(root, check_name=lambda name: _safe_names([name]))
                    same = _manifest((name, modes.get(name,mode) if os.name=="nt" else mode, content)
                                     for name,(mode,content) in actual.items()) == digest
                    if not same or environment != _runtime_digest(root,check):
                        raise ValueError("원격 검증 중 소스 또는 검증 환경이 변경됐습니다.")
                except (ValueError, OSError) as exc:
                    result.update(status="unverified",summary=str(exc),counts={})
                    cases = {}
                result.update(source_digest=digest, source_commit=commit,
                              environment={"platform":platform.platform(), "python":sys.version.split()[0],
                                           "digest":environment, "host":platform.node()},
                              recipe_argv=check["argv"], timeout_seconds=check["timeout_seconds"], finished_at=now())
                db.execute("UPDATE studio_peer_checks SET state='finished',result=?,cases=? WHERE ticket=?",
                           (encode(result), encode(cases), ticket))
                return result["status"] == "passed"
        finally:
            db.execute("UPDATE studio_peer_checks SET state='interrupted' WHERE ticket=? AND "
                       "state IN ('offered','claimed','running')", (ticket,))


def run_remote(store, run_id, attempt, position, workspace, revision, check, *, timeout, on_start):
    from .studio_ssh import RemoteHost, RemoteUnavailable, remote_request
    db = store.connection
    started = time.monotonic()
    result = dict(stage=check["stage"], host=None, target_host=check["host"], cwd=None, argv=check["argv"],
                  started_at=now(), elapsed_seconds=0, exit_code=None, status="unverified",
                  counts={}, scope_changes=[], summary="")
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_goal_peers'").fetchone():
            raise ValueError("등록된 SSH 검증 호스트가 없습니다.")
        remote = db.execute("""SELECT p.alias,p.port,p.journal_id FROM studio_goal_peer_targets t
            JOIN studio_goal_peers p USING(alias,port,journal_id) WHERE t.run_id=? AND t.requested=?""",
                            (run_id,check["host"])).fetchone()
        if remote is None:
            raise ValueError("이 Studio에 등록한 SSH 검증 호스트를 찾을 수 없습니다.")
        host = RemoteHost(*remote)
        selector = {"base":revision, "source":source_digest(workspace), "recipe":_selector(check),
                    "limit_ms":max(1, int(min(timeout,check["timeout_seconds"])*1000))}
        identifier = _digest([run_id, attempt, position])[:32]
        route = "/goal-checks/" + identifier
        result.update(peer_request=identifier, peer_journal=host.journal_id, source_digest=selector["source"])
        on_start(dict(result, status="running"))
        def poll():
            while True:
                try:
                    return remote_request(host, "GET", route)
                except RemoteUnavailable:
                    if time.monotonic()-started >= timeout:
                        raise
                    time.sleep(min(1., max(0.,timeout-(time.monotonic()-started))))
        # Only the first transmission is a POST. A lost acknowledgment never starts another job.
        try:
            code, reply = remote_request(host,"POST",route,selector)
        except RemoteUnavailable:
            code, reply = poll()
        while code == 200:
            if any(reply.get(k) != v for k,v in selector.items()) or reply.get("id") != identifier:
                raise ValueError("원격 검증 결과의 소스나 실행 식별자가 다릅니다.")
            if reply["state"] == "finished":
                received, cases = reply["result"], reply["cases"]
                if received.get("source_digest") != selector["source"] or received.get("recipe_argv") != check["argv"]:
                    raise ValueError("원격 검증 결과가 선택한 소스 또는 검증 범위와 다릅니다.")
                if source_digest(workspace) != selector["source"]:
                    raise ValueError("원격 검증 중 실행 호스트의 후보 소스가 변경됐습니다.")
                return dict(received, peer_request=identifier, peer_journal=host.journal_id,
                            dispatch_seconds=round(time.monotonic()-started,3)), cases
            if reply["state"] not in ("claimed","running") or time.monotonic() - started >= timeout:
                raise ValueError("원격 검증의 종료를 확인하지 못했습니다. 기존 요청 ID로 상태를 확인하세요.")
            time.sleep(.3)
            code, reply = poll()
        raise ValueError("원격 호스트에 동일한 기준 커밋·후보·검증 정의로 대기 중인 작업이 없습니다.")
    except (ValueError, OSError, RemoteUnavailable) as exc:
        result.update(summary=str(exc), elapsed_seconds=round(time.monotonic()-started,3))
        return result, {}

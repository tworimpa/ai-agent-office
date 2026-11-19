from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from agent_office import deployment, release_store, runtime_ops
from agent_office.managed_runtime import _load_slot
from agent_office.store import TaskStore
from agent_office.studio_server import StudioServer, _journal_id


def config(manager="systemd", service="office.service", port=8766):
    return dict(manager=manager, service=service, port=port, target_id="a" * 32,
                release_id="b" * 32, journal_id="c" * 32)


def request(port, route, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("GET", route, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@contextmanager
def launch(target, db, config, *options):
    # On Windows a venv redirector may be the immediate child. Closing this
    # pipe terminates the actual test interpreter before the redirector exits.
    supervisor = ("import os,sys,threading; "
                  "threading.Thread(target=lambda:(sys.stdin.buffer.read(),os._exit(0)),daemon=True).start(); "
                  "from unittest.mock import patch; "
                  "patch('agent_office.runtime_permissions.require_readonly').start(); "
                  "from agent_office.managed_runtime import main; sys.exit(main())")
    with subprocess.Popen(
        [sys.executable, "-c", supervisor, "--target", str(target), "--db", str(db), *options],
        cwd=db.parent, env={**os.environ, "PYTHONPATH": str(Path(deployment.__file__).resolve().parents[1])},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as process:
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(process.communicate()[1].decode(errors="replace"))
                result = runtime_ops.probe(config)
                if result["state"] == "healthy":
                    try:
                        yield result["identity"]
                    except Exception as exc:
                        process.stdin.close()
                        process.wait(timeout=10)
                        exc.add_note(process.stderr.read().decode(errors="replace"))
                        raise
                    return
                time.sleep(.1)
            pytest.fail(f"Managed Studio did not start: {result}")
        finally:
            process.stdin.close()
            process.wait(timeout=10)


@pytest.fixture
def staged(tmp_path):
    source = tmp_path / "source"
    package = source / "src" / "agent_office"
    shutil.copytree(Path(deployment.__file__).parent, package, ignore=shutil.ignore_patterns("__pycache__"))
    # Model an earlier Studio package, without the installed launcher's new modules.
    (package / "managed_runtime.py").unlink()
    (package / "runtime_ops.py").unlink()
    (package / "markers").mkdir()
    (package / "markers" / "build_marker.py").write_text('VALUE = "selected build"\n', encoding="utf-8")
    (package / "web" / "build.txt").write_bytes(b"selected asset")
    with (package / "studio_server.py").open("a", encoding="utf-8") as dst:
        dst.write("""
ASSETS["/fixture-asset"] = ("build.txt", "text/plain")
class StudioHandler(StudioHandler):
    def do_GET(self):
        if self.path != "/fixture-build":
            return super().do_GET()
        import subprocess, sys
        from . import store
        from .markers.build_marker import VALUE
        child = subprocess.check_output([sys.executable, "-c",
            "from agent_office import store; print(store.__file__)"], cwd=self.server.db_path.parent / "child-cwd",
            stdin=subprocess.DEVNULL, timeout=12)
        self._reply(200, dict(marker=VALUE, module=store.__file__, child=os.fsdecode(child.strip()),
            remote=self.server.accept_remote, view=str(self.server.release_view),
            source=str(self.server.improvement_source)))
""")
    release_store.run_git(source, "init", "--template=")
    release_store.run_git(source, "add", "--", "src")
    release_store.run_git(source, "commit", "-m", "Older compatible Studio")
    release = tmp_path / "release"
    sha = release_store.init_release(release, source, "HEAD", "sha256:" + "1" * 64)["stable"]
    target, db = tmp_path / "target", tmp_path / "journal.sqlite3"
    with TaskStore(db) as store:
        journal_id = _journal_id(store)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    cfg = deployment.init_target(release, target, manager="systemd", service="office.service",
                                 port=port, journal_id=journal_id)
    deployment._stage(target, release / "objects.git", sha)
    pick = dict(seq=1, sha=sha, activation_id=uuid4().hex)
    with deployment._db(target, write=True) as ledger:
        ledger.execute("UPDATE selection SET data=?", (json.dumps(pick),))
    return target, db, cfg, pick


def test_managed_boot_runs_selected_package_assets_and_children(staged):
    target, db, cfg, pick = staged
    view, source = db.parent / "view.sqlite3", db.parent / "source"
    shadow = db.parent / "child-cwd" / "agent_office"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise RuntimeError('foreign import')", encoding="utf-8")
    with launch(target, db, cfg, "--accept-remote", "--remote", "test-remote", "8766", "d" * 32,
                "--release-view", str(view), "--improvement-source", str(source)) as first:
        assert {key: first[key] for key in pick} == pick
        assert request(cfg["port"], "/fixture-asset") == (200, b"selected asset")
        built = json.loads(request(cfg["port"], "/fixture-build")[1])
        assert built["marker"] == "selected build" and built["remote"]
        store = target / "slots" / pick["sha"] / "src" / "agent_office" / "store.py"
        assert Path(built["module"]) == Path(built["child"]) == store
        assert Path(built["source"]) == source and Path(built["view"]) == view
        assert json.loads(request(cfg["port"], "/api/hosts")[1])["hosts"][1]["name"] == "test-remote"
        assert request(cfg["port"], "/api/runtime-health", {"Host": "foreign.example"})[0] == 403
        assert runtime_ops.probe({**cfg, "target_id": "0" * 32})["state"] == "wrong_identity"
    assert runtime_ops.probe(cfg)["state"] == "refused"
    with launch(target, db, cfg) as second:
        assert second["boot_id"] != first["boot_id"] and second["sha"] == first["sha"]
        with sqlite3.connect(db) as journal:
            journal.execute("UPDATE studio_identity SET id=?", (uuid4().hex,))
        assert request(cfg["port"], "/api/runtime-health")[0] == 503
        assert runtime_ops.probe(cfg)["state"] == "unhealthy"


def test_launcher_rejects_wrong_journal_and_tampered_slot(staged):
    target, db, cfg, pick = staged
    wrong = db.parent / "other.sqlite3"
    with TaskStore(wrong) as store:
        assert _journal_id(store) != cfg["journal_id"]
    command = [sys.executable, "-m", "agent_office.managed_runtime", "--target", str(target), "--db"]
    env = {**os.environ, "PYTHONPATH": str(Path(deployment.__file__).resolve().parents[1])}
    result = subprocess.run([*command, str(wrong)], cwd=db.parent, env=env, capture_output=True, timeout=15)
    assert result.returncode and b"existing runtime journal" in result.stderr
    result = subprocess.run([*command, str(db)], cwd=db.parent, env=env, capture_output=True, timeout=15)
    assert result.returncode and (b"modify the deployment target" in result.stderr or
                                  b"non-administrator service account" in result.stderr)
    (target / "slots" / pick["sha"] / "src" / "agent_office" / "markers" / "build_marker.py").write_bytes(b"FAKE")
    result = subprocess.run([*command, str(db)], cwd=db.parent, env=env, capture_output=True, timeout=15)
    assert result.returncode and b"differs from reviewed commit" in result.stderr
    assert runtime_ops.probe(cfg)["state"] == "refused"


@pytest.mark.parametrize("name", ["agent_office/runner.pyc", "agent_office/native.pyd", "sitecustomize.py"])
def test_managed_slot_rejects_cached_or_foreign_python(tmp_path, name):
    source = tmp_path / "src"
    package = source / "agent_office"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (source / name).touch()
    with pytest.raises(ValueError):
        _load_slot(tmp_path)


def test_unmanaged_studio_has_no_runtime_receipt(tmp_path):
    with StudioServer(tmp_path / "ordinary.sqlite3", 0) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            port = server.server_address[1]
            assert request(port, "/api/runtime-health")[0] == 404
            assert runtime_ops.probe(config(port=port))["state"] == "unhealthy"
        finally:
            server.shutdown()
            worker.join(timeout=5)


def test_hung_loopback_probe_is_bounded():
    gate = threading.Event()

    class Silent(BaseHTTPRequestHandler):
        def do_GET(self):
            gate.wait(3)

    with ThreadingHTTPServer(("127.0.0.1", 0), Silent) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            assert runtime_ops.probe(config(port=server.server_port), timeout=.2)["state"] == "timeout"
        finally:
            gate.set()
            server.shutdown()
            worker.join(timeout=5)


@pytest.mark.parametrize("manager,scope", [("systemd", "--system"), ("systemd-user", "--user")])
def test_systemd_fixed_commands(monkeypatch, manager, scope):
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = config(manager)
    seen = []
    def run(command, **options):
        assert options["timeout"] == 30 and options["stdin"] == subprocess.DEVNULL
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"LoadState=loaded\nActiveState=active\n")
    monkeypatch.setattr(subprocess, "run", run)
    runtime_ops.restart_service(cfg)
    prefix = ["/usr/bin/systemctl", scope, "--no-ask-password", "--no-pager"]
    assert seen == [[*prefix, "restart", "--", "office.service"],
                    [*prefix, "show", "--property=ActiveState", "--property=LoadState", "--", "office.service"]]
    for name in ("--all", "other.target", "../office.service", "*.service", "a.service\nstart"):
        with pytest.raises(ValueError):
            runtime_ops.restart_service({**cfg, "service": name})


def test_windows_stop_pending_before_start(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(runtime_ops, "_windows_sc", lambda: "SYSTEM/sc.exe")
    states = iter(["running", "stopping", "stopped", "starting", "running"])
    monkeypatch.setattr(runtime_ops, "_windows_state", lambda name: next(states))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    seen = []
    def run(command, **options):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"")
    monkeypatch.setattr(subprocess, "run", run)
    runtime_ops.restart_service(config("windows", "OfficeStudio"))
    assert seen == [["SYSTEM/sc.exe", "stop", "OfficeStudio"], ["SYSTEM/sc.exe", "start", "OfficeStudio"]]


def test_windows_paused_service_can_be_stopped_during_throttled_restart(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    states = iter(["pausing", "paused", "stopping", "stopped"])
    monkeypatch.setattr(runtime_ops, "_windows_state", lambda name: next(states))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    seen = []
    monkeypatch.setattr(runtime_ops, "_run", lambda cfg, action:
                        seen.append(action) or subprocess.CompletedProcess([], 0))
    runtime_ops.stop_service(config("windows", "OfficeStudio"))
    assert seen == ["stop"]


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel TCP and process tables")
def test_windows_verifies_real_listener_and_parent_process():
    from agent_office.windows_binding import owns_windows_process

    helper = ("import socket,sys,os; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
              "print(os.getpid(),s.getsockname()[1],flush=True); sys.stdin.buffer.read()")
    with subprocess.Popen([sys.executable, "-I", "-c", helper],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as worker:
        try:
            pid, port = map(int, worker.stdout.readline().split())
            assert owns_windows_process(pid, os.getpid(), port)
            assert owns_windows_process(None, os.getpid(), port)
            assert not owns_windows_process(os.getpid(), os.getpid(), port)
            assert not owns_windows_process(pid, 0, port)
        finally:
            worker.stdin.close()
            worker.wait(timeout=5)
    assert not owns_windows_process(pid, os.getpid(), port)


@pytest.mark.skipif(os.name != "nt", reason="Windows file ACLs")
def test_windows_checks_effective_write_rights(tmp_path):
    from agent_office.runtime_permissions import _windows_access

    try:
        with _windows_access() as access:
            assert access(tmp_path) & 2  # FILE_ADD_FILE
    except ValueError as exc:
        # Windows CI runners may hold a privileged token. Rejection is expected.
        assert "non-administrator service account" in str(exc)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and ACL checks")
def test_posix_least_privilege_checks_each_slot_file(tmp_path, monkeypatch):
    from agent_office import runtime_permissions

    target = tmp_path / "target"
    slot = target / "slots" / "selected"
    slot.mkdir(parents=True)
    (target / "deployment.sqlite3").touch()
    file = slot / "module.py"
    file.touch()
    writable = set()
    monkeypatch.setattr(os, "geteuid", lambda: 405001)
    monkeypatch.setattr(os, "access", lambda path, mode, effective_ids: path in writable)
    runtime_permissions.require_readonly(target, slot)
    for path in (file, slot, target / "deployment.sqlite3", target, tmp_path):
        writable.add(path)
        with pytest.raises(ValueError, match="modify"):
            runtime_permissions.require_readonly(target, slot)
        writable.clear()


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parsing")
def test_windows_reads_only_fixed_nssm_wrapper_fields(tmp_path, monkeypatch):
    from agent_office import windows_binding

    wrapper = tmp_path / "provisioned wrapper" / "nssm.exe"
    wrapper.parent.mkdir()
    wrapper.touch()
    target = tmp_path / "office target"
    journal = tmp_path / "run.sqlite3"
    args = ["-I", "-m", "agent_office.managed_runtime", "--target", str(target), "--db", str(journal)]
    cfg = config("windows", "OfficeStudio")
    monkeypatch.setattr(windows_binding, "_sc_config", lambda name: (f'"{wrapper}"', "fixture", 172))
    monkeypatch.setattr(windows_binding, "_account_sid", lambda name, service: "S-1-5-21-1000")
    monkeypatch.setattr(windows_binding, "_nssm_parameters",
                        lambda name: (sys.executable, subprocess.list2cmdline(args)))
    result = windows_binding.inspect_windows(cfg, target)
    assert result["pid"] == 172 and result["command"] == [sys.executable, *args]
    assert json.loads(result["binding"]) == [str(wrapper), "fixture", "S-1-5-21-1000"]
    monkeypatch.setattr(windows_binding, "_sc_config", lambda name: (f'"{wrapper}"', "LocalSystem", 172))
    with pytest.raises(ValueError, match="dedicated"):
        windows_binding.inspect_windows(cfg, target)
    monkeypatch.setattr(windows_binding, "_sc_config", lambda name: (sys.executable, "fixture", 172))
    with pytest.raises(ValueError, match="NSSM"):
        windows_binding.inspect_windows(cfg, target)

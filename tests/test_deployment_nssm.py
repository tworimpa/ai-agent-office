"""Real NSSM deployment on the disposable GitHub-hosted Windows runner."""
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
import os
from queue import Empty, Queue
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import time
from uuid import uuid4

import pytest

from agent_office import deployment, release_store, release_validation
from agent_office.release_view import sync_once
from agent_office.studio_server import StudioServer
from agent_office.windows_binding import inspect_windows, operator_sid
from test_deployment_systemd import CSS_MARKER, _copy_package, _promote
from test_release_store import IMAGE
from test_studio_http import request, studio


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("OFFICE_NSSM_ACCEPTANCE") != "1"
    or os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted",
    reason="requires the opted-in disposable GitHub-hosted Windows runner",
)


def _native(command):
    return subprocess.run(command, check=True, text=True, capture_output=True, timeout=30)


def _grant(path, sid, access):
    icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
    _native([icacls, path, "/grant:r", f"*{sid}:(OI)(CI){access}"])


@contextmanager
def _service(name, wrapper, python, target, journal, log):
    import pywintypes
    import win32service as scm
    import winreg

    account = "NT SERVICE\\" + name
    manager = scm.OpenSCManager(None, None, scm.SC_MANAGER_CREATE_SERVICE)
    try:
        service = scm.CreateService(
            manager, name, name, scm.SERVICE_ALL_ACCESS, scm.SERVICE_WIN32_OWN_PROCESS,
            scm.SERVICE_DEMAND_START, scm.SERVICE_ERROR_NORMAL, subprocess.list2cmdline([str(wrapper)]),
            None, 0, [], account, None,
        )
        try:
            settings = {
                "Application": str(python),
                "AppParameters": subprocess.list2cmdline(
                    ["-I", "-m", "agent_office.managed_runtime", "--target", str(target), "--db", str(journal)]
                ),
                "AppDirectory": str(journal.parent),
                "AppStderr": str(log),
                "AppNoConsole": 1,
                "AppRestartDelay": 120000,
            }
            # SCM creates the service; initialize NSSM's Parameters before its first start.
            with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE,
                                  rf"SYSTEM\CurrentControlSet\Services\{name}\Parameters") as params:
                for key, value in settings.items():
                    kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
                    winreg.SetValueEx(params, key, 0, kind, value)
            yield
        finally:
            try:
                if scm.QueryServiceStatus(service)[1] != scm.SERVICE_STOPPED:
                    try:
                        scm.ControlService(service, scm.SERVICE_CONTROL_STOP)
                    except pywintypes.error as exc:
                        if exc.winerror != 1062:
                            raise
                    deadline = time.monotonic() + 20
                    while scm.QueryServiceStatus(service)[1] != scm.SERVICE_STOPPED:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("NSSM fixture service did not stop")
                        time.sleep(0.2)
            finally:
                try:
                    scm.DeleteService(service)
                finally:
                    scm.CloseServiceHandle(service)
    finally:
        scm.CloseServiceHandle(manager)


def _deploy(python, release, target, config, seq, sha, previous, expected_code):
    from winpty import PtyProcess

    origin = f"{previous['seq']} {previous['sha']} {previous['boot_id']}" if previous else "refused"
    phrase = f"deploy {config['target_id']} windows {config['service']} {origin} to {seq} {sha}"
    args = [str(python), "-I", "-m", "agent_office.cli", "improve", "--release", str(release),
            "deploy", str(seq), "--target", str(target)]
    process = PtyProcess.spawn(args, dimensions=(40, 512))
    chunks = Queue()

    def read_console():
        try:
            while True:
                text = process.read(4096)
                if not text:
                    break
                chunks.put(text)
        except EOFError:
            pass
        finally:
            chunks.put(None)

    reader = Thread(target=read_console, daemon=True)
    reader.start()
    screen = ""
    confirmed = False
    deadline = time.monotonic() + 155
    try:
        while time.monotonic() < deadline:
            try:
                chunk = chunks.get(timeout=1)
            except Empty:
                continue
            if chunk is None:
                break
            screen += chunk
            if not confirmed and phrase in screen:
                process.write(phrase + "\r")
                confirmed = True
        while process.isalive() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not process.isalive(), screen[-16000:]
        process.wait()
        assert confirmed and process.exitstatus == expected_code, screen[-16000:]
    finally:
        process.close(force=True)
        reader.join(timeout=3)


def _running(target, seq, sha, account_sid):
    import win32api
    import win32con
    import win32security

    status = deployment.public_status(target)
    assert status["health"] == "healthy", status
    receipt = status["actual"]
    assert (receipt["seq"], receipt["sha"]) == (seq, sha)
    process = win32api.OpenProcess(0x1000, False, receipt["pid"])
    try:
        token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        try:
            sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        finally:
            token.Close()
    finally:
        process.Close()
    assert win32security.ConvertSidToStringSid(sid) == account_sid != operator_sid()
    connection = HTTPConnection("127.0.0.1", status["port"], timeout=5)
    try:
        connection.request("GET", "/versions.css")
        response = connection.getresponse()
        assert response.status == 200
        assert (CSS_MARKER.encode() in response.read()) == (seq == 2)
    finally:
        connection.close()
    return receipt


def _check_browser(desk, third, second, receipt):
    from playwright.sync_api import expect, sync_playwright

    code, dashboard = request(desk, f"/api/deployment/versions/3/{third}")
    assert code == 200 and dashboard["release_matches"] is True and dashboard["health"] == "healthy"
    assert dashboard["running"] == {"seq": 2, "sha": second, "boot_id": receipt["boot_id"]}
    assert dashboard["last"]["state"] == "deploy_failed_restored"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", chromium_sandbox=True)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{desk.server_port}/versions")
            expect(page.locator("#to-version")).to_have_value("3")
            expect(page.locator("#current-sha")).to_have_text(third)
            expect(page.locator("#running-version")).to_have_text("#2")
            expect(page.locator("#running-sha")).to_have_text(second)
            expect(page.locator("#deployment-last")).to_contain_text("이전 버전 복구 완료")
            expect(page.locator("#deployment-match")).to_contain_text("실행 중인 커밋이 다릅니다.")
            page.locator("#to-version").select_option("2")
            expect(page.locator("#deployment-match")).to_have_text("대상 #2 · 실행 버전과 일치합니다.")
        finally:
            browser.close()


def test_nssm_restarts_and_restores_previous_boot_visible_in_studio(monkeypatch):
    wrapper_source = Path(os.environ["OFFICE_NSSM_EXE"]).resolve(strict=True)

    def compile_fixture(repo, revision, image):
        for name, _, blob in release_validation.committed_files(repo, revision):
            if name.endswith(".py"):
                compile(blob, name, "exec")
        return dict(sha=revision, image=image, passed=True, exit_code=0, output="Fixture source compiled")

    # Candidate preparation uses a local syntax check. Runtime, OS and browser paths are real.
    monkeypatch.setattr(release_validation, "validate_candidate", compile_fixture)
    with tempfile.TemporaryDirectory(prefix="office-nssm-", dir=Path(os.environ["SystemDrive"] + "\\")) as tmp:
        scratch = Path(tmp)
        icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
        _native([icacls, scratch, "/inheritance:r", "/grant:r", f"*{operator_sid()}:(OI)(CI)F",
                 "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"])
        wrapper = scratch / "nssm.exe"
        shutil.copyfile(wrapper_source, wrapper)
        source, release, target = scratch / "source", scratch / "release", scratch / "target"
        runtime_data = scratch / "data"
        runtime_data.mkdir()
        journal, log = runtime_data / "tasks.sqlite3", runtime_data / "startup.log"
        with StudioServer(journal, port=0) as runtime:
            port, journal_id = runtime.server_port, runtime.journal_id
        environment = scratch / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", environment],
                       check=True, timeout=60)
        python = environment / "Scripts" / "python.exe"
        purelib = _native([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).stdout.strip()
        _copy_package(Path(purelib) / "agent_office")
        _copy_package(source / "src" / "agent_office")
        # Git's fixture plumbing disables autocrlf. Match unified diff context to the fixture bytes.
        css_path, server_path = "src/agent_office/web/versions.css", "src/agent_office/studio_server.py"
        for path in (source / css_path, source / server_path):
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
        release_store.run_git(source, "init", "--template=")
        release_store.run_git(source, "add", "--", "src")
        release_store.run_git(source, "commit", "-m", "Disposable Studio source")
        first = release_store.init_release(release, source, "HEAD", IMAGE)["stable"]
        name = "office-nssm-test-" + uuid4().hex
        config = deployment.init_target(
            release, target, manager="windows", service=name, port=port, journal_id=journal_id, runtime_python=python,
        )

        with _service(name, wrapper, python, target, journal, log):
            account_sid = inspect_windows(config, target)["account_sid"]
            _grant(scratch, account_sid, "RX")
            _grant(target, account_sid, "RX")
            _grant(runtime_data, account_sid, "M")
            try:
                _deploy(python, release, target, config, 1, first, None, 0)
                first_boot = _running(target, 1, first, account_sid)
                css = (source / css_path).read_text(encoding="utf-8")
                second = _promote(release, first, css_path, css, css + CSS_MARKER + "\n")
                _deploy(python, release, target, config, 2, second, first_boot, 0)
                second_boot = _running(target, 2, second, account_sid)
                assert second_boot["boot_id"] != first_boot["boot_id"]

                server = (source / server_path).read_text(encoding="utf-8")
                failure = "disposable Windows Studio startup failure"
                broken = server.replace("from http.server import", f"raise RuntimeError({failure!r})\nfrom http.server import", 1)
                assert broken != server
                third = _promote(release, second, server_path, server, broken)
                _deploy(python, release, target, config, 3, third, second_boot, 1)
                assert f"Managed Studio startup failed: {failure}" in log.read_text(encoding="utf-8", errors="replace")
                restored = _running(target, 2, second, account_sid)
                assert restored["boot_id"] not in {first_boot["boot_id"], second_boot["boot_id"]}
                assert restored["activation_id"] != second_boot["activation_id"]
                assert release_store.release_status(release)["stable"] == third
                management, view = scratch / "management.sqlite3", scratch / "publication" / "view.sqlite3"
                with studio(management, release_view=view, deployment_target=target) as desk:
                    sync_once(management, release, view, stable_history=True)
                    _check_browser(desk, third, second, restored)
            except Exception:
                if log.is_file():
                    print(log.read_bytes()[-5000:].decode("utf-8", errors="replace"))
                raise

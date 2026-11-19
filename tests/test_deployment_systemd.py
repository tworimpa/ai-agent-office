"""Native deployment acceptance on an explicitly opted-in disposable Linux VM."""
from contextlib import contextmanager
from difflib import unified_diff
import errno
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import pytest

from agent_office import deployment, release_store
from agent_office.release_view import sync_once
from test_release_store import proposal
from test_studio_http import request, studio


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("OFFICE_SYSTEMD_ACCEPTANCE") != "1",
    reason="requires an opted-in disposable VM with systemd as the system manager",
)

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "agent_office"
CSS_MARKER = "/* disposable stable version 2 */"


def _copy_package(destination):
    shutil.copytree(PACKAGE, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _promote(release, base, path, before, after):
    patch = unified_diff(before.splitlines(True), after.splitlines(True), "a/" + path, "b/" + path)
    candidate = release_store.submit(release, proposal(base) | {"patch": "".join(patch)})
    verified = release_store.verify_candidate(release, candidate["id"])
    assert verified["state"] == "passed", verified
    assert release_store.decide(release, candidate["id"], candidate["sha"], "approve")["state"] == "approved"
    return candidate["sha"]


def _runtime(scratch):
    import pwd

    account = pwd.getpwnam("nobody")
    environment = scratch / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", environment],
                   check=True, timeout=60)
    python = environment / "bin" / "python"
    purelib = subprocess.check_output([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                      text=True, timeout=20).strip()
    _copy_package(Path(purelib) / "agent_office")
    data = scratch / "runtime-data"
    data.mkdir(mode=0o700)
    os.chown(data, account.pw_uid, account.pw_gid)
    journal = data / "tasks.sqlite3"
    # Initialize under the same account as the future service, including its lock files.
    initialize = (
        "import json, sys\nfrom pathlib import Path\n"
        "from agent_office.studio_server import StudioServer\n"
        "with StudioServer(Path(sys.argv[1]), port=0) as server:\n"
        "    print(json.dumps([server.journal_id, server.server_port]))\n"
    )
    initialized = subprocess.check_output(
        [python, "-I", "-c", initialize, journal], cwd=scratch, user=account.pw_uid,
        group=account.pw_gid, extra_groups=[], text=True, timeout=45,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(data)},
    )
    journal_id, port = json.loads(initialized)
    return python, journal, journal_id, port


@contextmanager
def _managed_service(unit, python, target, journal):
    service, timer = unit + ".service", unit + ".timer"
    subprocess.run(
        ["/usr/bin/systemd-run", "--system", "--unit=" + unit, "--on-active=24h", "--service-type=exec",
         "--property=User=nobody", "--property=Group=nogroup", "--",
         python, "-I", "-m", "agent_office.managed_runtime", "--target", target, "--db", journal],
        check=True, timeout=30,
    )
    try:
        yield
    except BaseException:
        subprocess.run(["/usr/bin/journalctl", "--no-pager", "-n", "40", "--unit", service], timeout=10)
        raise
    finally:
        subprocess.run(["/usr/bin/systemctl", "--system", "stop", "--", timer, service],
                       check=True, timeout=45)


def _deploy(python, release, target, config, seq, sha, previous, expected_code):
    """Wait for the actual reviewed phrase on the terminal, then confirm exactly that version."""
    origin = (f"{previous['seq']} {previous['sha']} {previous['boot_id']}" if previous else "refused")
    phrase = f"deploy {config['target_id']} systemd {config['service']} {origin} to {seq} {sha}".encode()
    command = [python, "-I", "-m", "agent_office.cli", "improve", "--release", release,
               "deploy", str(seq), "--target", target]
    master, slave = os.openpty()
    try:
        with subprocess.Popen(command, stdin=slave, stderr=slave, stdout=subprocess.PIPE) as process:
            os.close(slave)
            slave = -1
            confirmed = False
            screen = bytearray()
            deadline = time.monotonic() + 150
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and select.select([master], [], [], remaining)[0], screen.decode(errors="replace")
                    try:
                        data = os.read(master, 8192)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break  # Linux master after the last slave closed.
                        raise
                    if not data:
                        break
                    screen.extend(data)
                    if not confirmed and phrase in screen.splitlines():
                        os.write(master, phrase + b"\n")
                        confirmed = True
                stdout, _ = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
            assert confirmed and process.returncode == expected_code, (screen.decode(errors="replace"), stdout)
            return json.loads(stdout)
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)


def _running(config, target, seq, sha):
    status = deployment.public_status(target)
    assert status["health"] == "healthy", status
    receipt = status["actual"]
    assert (receipt["seq"], receipt["sha"]) == (seq, sha)
    main_pid = subprocess.check_output(
        ["/usr/bin/systemctl", "show", "--value", "--property=MainPID", "--", config["service"]],
        text=True, timeout=5,
    )
    assert receipt["pid"] == int(main_pid) > 0
    connection = HTTPConnection("127.0.0.1", config["port"], timeout=5)
    try:
        connection.request("GET", "/versions.css")
        response = connection.getresponse()
        assert response.status == 200
        assert (CSS_MARKER.encode() in response.read()) == (seq == 2)
    finally:
        connection.close()
    return receipt


def test_two_native_deployments_and_failed_upgrade_restore(trusted_validator_image):
    assert os.geteuid() == 0, "run only as the operator on the disposable VM"
    with tempfile.TemporaryDirectory(prefix="office-systemd-", dir="/tmp") as temp:
        scratch = Path(temp)
        scratch.chmod(0o755)
        python, journal, journal_id, port = _runtime(scratch)
        source, release, target = scratch / "source", scratch / "release", scratch / "target"
        _copy_package(source / "src" / "agent_office")
        release_store.run_git(source, "init", "--template=")
        release_store.run_git(source, "add", "--", "src")
        release_store.run_git(source, "commit", "-m", "Disposable Studio source")
        first = release_store.init_release(release, source, "HEAD", trusted_validator_image)["stable"]
        unit = "office-test-" + uuid4().hex
        config = deployment.init_target(release, target, manager="systemd", service=unit + ".service",
                                        port=port, journal_id=journal_id, runtime_python=python)
        target.chmod(0o755)
        (target / "slots").chmod(0o755)
        (target / deployment.DB_NAME).chmod(0o644)

        with _managed_service(unit, python, target, journal):
            assert _deploy(python, release, target, config, 1, first, None, 0)["state"] == "healthy"
            first_boot = _running(config, target, 1, first)

            css_path = "src/agent_office/web/versions.css"
            css = (source / css_path).read_text(encoding="utf-8")
            second = _promote(release, first, css_path, css, css + CSS_MARKER + "\n")
            assert _deploy(python, release, target, config, 2, second, first_boot, 0)["state"] == "healthy"
            second_boot = _running(config, target, 2, second)
            assert second_boot["boot_id"] != first_boot["boot_id"]

            # Compiles and passes the real validator. Only Studio startup exercises this import.
            server_path = "src/agent_office/studio_server.py"
            server = (source / server_path).read_text(encoding="utf-8")
            injection = 'raise RuntimeError("disposable Studio startup failure")\n'
            broken = server.replace("from http.server import", injection + "from http.server import", 1)
            third = _promote(release, second, server_path, server, broken)
            failed = _deploy(python, release, target, config, 3, third, second_boot, 1)
            assert failed["state"] == "deploy_failed_restored"
            unit_log = subprocess.check_output(
                ["/usr/bin/journalctl", "--no-pager", "--output=cat", "--unit", config["service"]],
                text=True, timeout=10,
            )
            assert "Managed Studio startup failed: disposable Studio startup failure" in unit_log, unit_log
            restored = _running(config, target, 2, second)
            assert restored["boot_id"] not in (first_boot["boot_id"], second_boot["boot_id"])
            assert restored["activation_id"] != second_boot["activation_id"]
            assert release_store.release_status(release)["stable"] == third

            management, view = scratch / "management.sqlite3", scratch / "publication" / "view.sqlite3"
            with studio(management, release_view=view, deployment_target=target) as desk:
                sync_once(management, release, view, stable_history=True)
                route = f"/api/deployment/versions/3/{third}"
                code, dashboard = request(desk, route)
                assert code == 200 and dashboard["release_matches"] is True and dashboard["health"] == "healthy"
                assert dashboard["selection"] == dashboard["last"]["requested"] == {"seq": 3, "sha": third}
                assert dashboard["running"] == {"seq": 2, "sha": second, "boot_id": restored["boot_id"]}
                assert dashboard["last"]["state"] == failed["state"]
                assert request(desk, route, {})[0] == 404

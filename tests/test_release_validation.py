import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

import pytest

from agent_office import release_validation as validation


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sha256:" + "a" * 64


def git(repo, *args, input=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=input, check=True,
                          capture_output=True).stdout.strip().decode()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.test")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitattributes").write_bytes(b"hidden export-ignore\nrun export-subst\n")
    (repo / "hidden").write_bytes(b"archive-ignored\0bytes")
    (repo / "run").write_bytes(b"$Format:%H$\n")
    git(repo, "add", ".")
    git(repo, "update-index", "--chmod=+x", "run")
    git(repo, "commit", "-qm", "snapshot")
    return repo


def test_snapshot_preserves_raw_bytes_and_mode_despite_attributes_and_dirty_worktree(repo):
    sha = git(repo, "rev-parse", "HEAD")
    (repo / "hidden").write_bytes(b"changed")
    (repo / "extra").write_bytes(b"new")
    files = validation.committed_files(repo / ".git", sha)
    assert files == [
        (".gitattributes", 0o644, b"hidden export-ignore\nrun export-subst\n"),
        ("hidden", 0o644, b"archive-ignored\0bytes"),
        ("run", 0o755, b"$Format:%H$\n"),
    ]
    git(repo, "commit", "-qam", "different content")
    git(repo, "replace", sha, "HEAD")
    assert validation.committed_files(repo / ".git", sha) == files


@pytest.mark.parametrize(("mode", "path"), [
    ("120000", "link"), ("100644", "aux.txt"), ("100644", "dir/long~1"),
    ("100644", "HiDdEn"),
])
def test_unrepresentable_tree_fails_closed(repo, mode, path):
    blob = git(repo, "hash-object", "-w", "--stdin", input=b"data")
    git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")
    tree = git(repo, "write-tree")
    sha = git(repo, "commit-tree", tree, "-m", "candidate")
    with pytest.raises(ValueError):
        validation.committed_files(repo / ".git", sha)


@pytest.mark.parametrize("image", ["latest", "repo@sha256:" + "a" * 64, "sha256:example"])
def test_only_explicit_local_immutable_image_ids(repo, image):
    result = validation.validate_candidate(repo / ".git", git(repo, "rev-parse", "HEAD"), image)
    assert not result["passed"] and result["exit_code"] is None
    assert "immutable image ID" in result["output"]


@pytest.mark.parametrize("scenario", ["pass", "reject", "timeout", "bad image", "missing docker",
                                      "cleanup", "unowned"])
def test_container_invocation_and_failure_evidence(repo, monkeypatch, scenario):
    sha = git(repo, "rev-parse", "HEAD")
    execute = validation._execute
    calls = []
    name, owner = validation._container_identity(repo / ".git", sha, IMAGE)
    container_id = "1" * 64

    def fake(argv, **kwargs):
        if argv[0] != "docker":
            return execute(argv, **kwargs)
        calls.append(argv)
        assert kwargs["env"].get("DOCKER_CONFIG") is None
        if "image" in argv:
            if scenario == "bad image":
                return 1, b"missing"
            if scenario == "missing docker":
                raise FileNotFoundError("docker is missing")
            config = {"Entrypoint": ["python"], "Cmd": ["suite.py"]}
            return 0, json.dumps([{"Id": IMAGE, "Os": "linux", "Config": config}]).encode()
        if "container" in argv:
            assert argv[-1] == name
            marker = "unrelated" if scenario == "unowned" else owner
            return 0, f"{container_id} /{name} {marker}".encode()
        if "rm" in argv:
            assert argv[-1] == container_id
            code = int(scenario == "cleanup" and any("run" in call for call in calls))
            return code, b"daemon unavailable"
        if "run" in argv:
            assert {"--cap-drop=ALL", "--pull=never", "--interactive", "--network=none",
                    "--security-opt=no-new-privileges", "--read-only", "--user=65534:65534",
                    "--pids-limit=64", "--cpus=1", "--memory=256m", "--log-driver=none"} <= set(argv)
            assert not {"--mount", "-v", "--volume", "--privileged", "--env", "-e"}.intersection(argv)
            assert kwargs["timeout"] == validation.RUN_TIMEOUT + 15
            assert kwargs["limit"] == validation.MAX_OUTPUT
            assert argv[-6:] == [IMAGE, "--signal=KILL", "--", f"{validation.RUN_TIMEOUT}s",
                                 "python", "suite.py"]
            assert "--entrypoint=/usr/bin/timeout" in argv
            assert argv[argv.index("--name") + 1] == name
            assert argv[argv.index("--label") + 1] == f"{validation.OWNER_LABEL}={owner}"
            assert "rm" in calls[-2]  # Recovered the previous attempt before launching.
            with tarfile.open(fileobj=io.BytesIO(kwargs["source"].read())) as archive:
                assert archive.extractfile("hidden").read() == b"archive-ignored\0bytes"
                assert archive.getmember("run").mode == 0o755
            if scenario == "timeout":
                raise TimeoutError("docker timed out")
            return (1 if scenario == "reject" else 0), b"suite result"
        pytest.fail(f"Unexpected Docker invocation: {argv}")

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(validation, "_execute", fake)
    result = validation.validate_candidate(repo / ".git", sha, IMAGE)
    assert result["sha"] == sha and result["image"] == IMAGE
    assert result["passed"] is (scenario == "pass")
    assert ("rm" in calls[-1]) is (scenario not in ("bad image", "missing docker", "unowned"))
    if scenario == "unowned":
        assert all("rm" not in call and "run" not in call for call in calls)
    if scenario in ("pass", "reject"):
        assert result["exit_code"] == (0 if scenario == "pass" else 1)
        assert result["output"] == "suite result"
    else:
        assert result["output"]


def test_remote_docker_host_rejected(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "ssh://remote-machine")
    with pytest.raises(ValueError, match="local Docker"):
        validation._local_daemon()


def test_output_and_execution_are_bounded():
    with pytest.raises(ValueError, match="output exceeded"):
        validation._execute([sys.executable, "-c", "print('x' * 1000000)"],
                            timeout=5, limit=512, env=validation._tool_env())
    with pytest.raises(TimeoutError, match="timed out"):
        validation._execute([sys.executable, "-c", "import time; time.sleep(10)"],
                            timeout=0.1, limit=512, env=validation._tool_env())


def test_real_trusted_image_accepts_baseline_and_rejects_regression(repo, trusted_validator_image):
    image = trusted_validator_image
    package = repo / "src" / "agent_office"
    package.mkdir(parents=True)
    for filename in ("__init__.py", "models.py", "engines.py", "events.py"):
        shutil.copyfile(ROOT / "src" / "agent_office" / filename, package / filename)
    # These assertions run inside the real validation container on each probe.
    with (package / "__init__.py").open("a", encoding="utf-8") as init:
        init.write("\nimport os\nfrom pathlib import Path\n"
                   "assert os.getuid() == 65534\n"
                   "assert os.statvfs('/').f_flag & os.ST_RDONLY\n"
                   "assert set(os.listdir('/sys/class/net')) == {'lo'}\n"
                   "assert not Path('/var/run/docker.sock').exists()\n")
    git(repo, "add", "src")
    git(repo, "commit", "-qm", "baseline")
    baseline = validation.validate_candidate(repo / ".git", git(repo, "rev-parse", "HEAD"), image)
    assert baseline["passed"] and baseline["exit_code"] == 0, baseline["output"]
    assert "Passed: request redaction" in baseline["output"]
    with (package / "events.py").open("a", encoding="utf-8") as events:
        events.write("\ndef snapshot_request(text):\n    return text\n")
    git(repo, "commit", "-qam", "regression")
    failure = validation.validate_candidate(repo / ".git", git(repo, "rev-parse", "HEAD"), image)
    assert not failure["passed"] and failure["exit_code"] == 1, failure["output"]
    assert "Failed regression: request redaction" in failure["output"]


def test_container_deadline_survives_killed_supervisor(repo, trusted_validator_image):
    package = repo / "src" / "agent_office"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("import time\ntime.sleep(60)\n")
    git(repo, "add", "src")
    git(repo, "commit", "-qm", "slow candidate")
    sha = git(repo, "rev-parse", "HEAD")
    _, owner = validation._container_identity(repo / ".git", sha, trusted_validator_image)
    docker = validation.resolve_executable("docker.exe" if os.name == "nt" else "docker")
    command = [docker, "--host", validation._local_daemon(), "container", "ls", "--quiet",
               "--filter", f"label={validation.OWNER_LABEL}={owner}"]

    def wait_until_running(expected):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            args = command if expected else [*command, "--all"]
            if bool(subprocess.check_output(args, timeout=5).strip()) == expected:
                return
            time.sleep(0.15)
        pytest.fail(f"Container did not reach running={expected}")

    script = ("from pathlib import Path\nimport sys\nfrom agent_office import release_validation as v\n"
              "v.RUN_TIMEOUT = 4\n"
              "print(v.validate_candidate(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))\n")
    with subprocess.Popen([sys.executable, "-c", script, str(repo / ".git"), sha, trusted_validator_image],
                          env={**os.environ, "PYTHONPATH": str(ROOT / "src")}) as supervisor:
        try:
            wait_until_running(True)
        finally:
            supervisor.kill()
            supervisor.wait(timeout=5)
    wait_until_running(False)

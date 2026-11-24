"""Keep the original personal 0.8.1 distinct from later main builds of that version."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


@pytest.fixture
def baseline(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import desktop_upgrade_baseline
    return desktop_upgrade_baseline


def test_baseline_pins_member_and_archive_and_rejects_drift(baseline, monkeypatch, tmp_path):
    data = b"fixture installer"
    archive = io.BytesIO()
    pin = dict(baseline.BASELINES["Linux"])
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr(pin["installer"], data)
    archive = archive.getvalue()
    pin.update(archive_sha256=hashlib.sha256(archive).hexdigest(), installer_sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setitem(baseline.BASELINES, "Linux", pin)
    endpoint = f"actions/artifacts/{pin['artifact_id']}"
    metadata = dict(id=pin["artifact_id"], expired=False, digest="sha256:" + pin["archive_sha256"],
                    workflow_run=dict(id=baseline.RUN, head_branch="main", head_sha=baseline.COMMIT))
    def api(path, output=None):
        if path == endpoint:
            return subprocess.CompletedProcess([], 0, json.dumps(metadata))
        assert path == endpoint + "/zip"
        output.write(archive)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(baseline, "gh_api", api)
    def recover(directory):
        monkeypatch.setattr(sys, "argv", ["baseline", "--platform", "Linux", "--out", str(tmp_path / directory)])
        baseline.main()
    recover("good")
    receipt = json.loads((tmp_path / "good" / "baseline.json").read_text())
    assert receipt["source"] == "original-tested-archive" and receipt["installed_sha256"] == pin["installer_sha256"]
    assert (tmp_path / "good" / pin["installer"]).read_bytes() == data
    pin["installer_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="installer checksum"):
        recover("member")
    archive += b"changed"
    with pytest.raises(ValueError, match="archive checksum"):
        recover("archive")
    metadata["workflow_run"]["id"] += 1
    with pytest.raises(ValueError, match="identity"):
        recover("run")


@pytest.mark.parametrize(("status", "rebuild"), [(404, True), (410, True), (403, False), (500, False)])
def test_only_missing_artifacts_permit_a_source_rebuild(baseline, monkeypatch, tmp_path, status, rebuild):
    def unavailable(*args, **kwargs):
        return subprocess.CompletedProcess([], 1, stderr=f"gh: error (HTTP {status})".encode())
    monkeypatch.setattr(subprocess, "run", unavailable)
    monkeypatch.setattr(sys, "argv", ["baseline", "--platform", "Windows", "--out", str(tmp_path)])
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    if rebuild:
        baseline.main()
        assert output.read_text().splitlines() == [
            "rebuild=true", "source-commit=bbf4e06ffad8b66c596b4bda30aab46abf0ef99b",
        ]
    else:
        with pytest.raises(RuntimeError, match="API request failed"):
            baseline.main()
        assert not output.exists()

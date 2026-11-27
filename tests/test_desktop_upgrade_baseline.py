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


def test_public_upgrade_baseline_verifies_the_released_assets(baseline, monkeypatch, tmp_path):
    from desktop_release import CHECKSUMS, checksum_bytes, installer_names

    names = installer_names("0.9.0")
    files = {names["Windows"]: b"windows fixture", names["Linux"]: b"linux fixture"}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files[CHECKSUMS] = checksum_bytes(hashes)
    hashes[CHECKSUMS] = hashlib.sha256(files[CHECKSUMS]).hexdigest()
    monkeypatch.setattr(baseline, "PUBLIC_HASHES", hashes)
    ids = {name: n for n, name in enumerate(files, 10)}
    monkeypatch.setattr(baseline, "PUBLIC_ASSET_IDS", ids)

    class Fixture:
        def get(self, route):
            assert route == "releases/tags/v0.9.0"
            return {"tag_name": "v0.9.0", "draft": False, "prerelease": False,
                    "target_commitish": baseline.PUBLIC_COMMIT, "id": baseline.PUBLIC_RELEASE_ID}

        def items(self, route):
            if route == "git/matching-refs/tags/v0.9.0":
                return [{"ref": "refs/tags/v0.9.0", "object": {"type": "commit", "sha": baseline.PUBLIC_COMMIT}}]
            assert route == f"releases/{baseline.PUBLIC_RELEASE_ID}/assets?per_page=100"
            return [{"id": ids[n], "name": n, "state": "uploaded", "digest": f"sha256:{digest}"}
                    for n, digest in hashes.items()]

        def download(self, route, target):
            asset = int(route.rsplit("/", 1)[-1])
            name = next(name for name, value in ids.items() if value == asset)
            target.write_bytes(files[name])

    path = tmp_path / "valid"
    path.mkdir()
    receipt = baseline.published_baseline(Fixture(), "Windows", path)
    assert receipt["version"] == "0.9.0"
    assert receipt["source"] == "published-release"
    assert receipt["commit"] == baseline.PUBLIC_COMMIT
    assert receipt["historical_artifact"]["installer"] == names["Windows"]
    assert receipt["installed_sha256"] == hashes[names["Windows"]]
    assert (path / names["Windows"]).read_bytes() == files[names["Windows"]]

    path = tmp_path / "modified"
    path.mkdir()
    files[names["Linux"]] = b"not the released package"
    with pytest.raises(ValueError, match="Published installer checksum mismatch"):
        baseline.published_baseline(Fixture(), "Linux", path)
    assert not (path / names["Linux"]).exists()

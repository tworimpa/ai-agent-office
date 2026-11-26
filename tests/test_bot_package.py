from io import BytesIO
import json
import subprocess
import time
from zipfile import ZipFile

import pytest

from agent_office import bot_package, git_bot_source


def definition():
    return {
        "specVersion": "1.0", "id": "shared-research", "name": "공유 조사 봇", "role": "리서처",
        "files": {
            "SOUL.md": "공개 자료를 읽으세요.",
            "MEMORY.md": "비공개 원본 메모",
            "nested/memory.md": "비공개 중첩 메모",
            "references/source.md": "공개 참고 자료",
            "skills/summarize/SKILL.md": "# 요약",
        },
        "folders": ["notes"],
        "extensions": {"org.example.extra": {"value": True}},
    }


def test_package_export_and_validation_preserve_shared_definition():
    source = definition()
    with ZipFile(BytesIO(bot_package.export_zip(source))) as archive:
        assert archive.namelist() == ["plugin.json", bot_package.DEFINITION_PATH]
        definition_bytes = archive.read(bot_package.DEFINITION_PATH)
        assert "비공개".encode() not in definition_bytes
        assert b"MEMORY.md" not in definition_bytes
        package = bot_package.read(archive.read("plugin.json"), definition_bytes, {})
    assert package.definition["files"] == {
        key: value for key, value in source["files"].items() if not key.lower().endswith("memory.md")
    }
    assert package.definition["folders"] == ["notes"]
    assert package.definition["extensions"] == source["extensions"]
    assert source["files"]["MEMORY.md"] == "비공개 원본 메모"


def test_handwritten_packages_reject_memos_and_duplicate_skill_paths():
    with ZipFile(BytesIO(bot_package.export_zip(definition()))) as archive:
        plugin = archive.read("plugin.json")
        payload = archive.read(bot_package.DEFINITION_PATH)
    with pytest.raises(ValueError, match="MEMORY.md"):
        bot_package.read(plugin, json.dumps(definition()).encode(), {})
    with pytest.raises(ValueError, match="중복"):
        bot_package.read(plugin, payload, {"skills/summarize/SKILL.md": b"# duplicate"})
    with pytest.raises(ValueError, match="MEMORY.md"):
        bot_package.read(plugin, payload, {"skills/MEMORY.md/SKILL.md": b"# incompatible path"})
    package = bot_package.read(plugin, payload, {"skills/outline/SKILL.md": b"# outline"})
    assert package.definition["files"]["skills/outline/SKILL.md"] == "# outline"


def test_git_source_limits_ref_to_tags_or_full_commit():
    url = "https://example.org/team/bots.git"
    assert git_bot_source.Source.parse({"url": url, "version": "v1.0", "path": ""}).ref == "refs/tags/v1.0"
    assert git_bot_source.Source.parse({"url": url, "version": "a" * 40}).ref == "a" * 40
    with pytest.raises(ValueError):
        git_bot_source.Source.parse({"url": url, "version": "refs/heads/main"})
    with pytest.raises(ValueError):
        git_bot_source.Source.parse({"url": url, "version": "v1.0", "path": "../office"})


@pytest.mark.parametrize("url", [
    "http://example.org/repo", "https://example.org:8443/repo", "https://user@example.org/repo",
    "https://example.org/repo?token=no", "https://example.org/repo#branch",
    "https://example.org/repo/%2e%2e", "https://127.0.0.1/repo",
])
def test_git_source_rejects_non_public_https_url_forms(url):
    with pytest.raises(ValueError):
        git_bot_source.Source.parse({"url": url, "version": "v1"})


def test_git_network_options_pin_dns_and_reject_nonpublic_addresses():
    assert git_bot_source._network_options("example.org", ["93.184.215.14"]) == [
        "-c", "http.curloptResolve=example.org:443:93.184.215.14"
    ]
    with pytest.raises(ValueError):
        git_bot_source._network_options("example.org", ["93.184.215.14", "127.0.0.1"])


def test_dns_lookup_is_owned_by_the_package_deadline(tmp_path, monkeypatch):
    repository = git_bot_source._Repository(tmp_path)
    monkeypatch.setattr(git_bot_source, "_DNS_LOOKUP", "import time; time.sleep(10)")
    repository.deadline = time.monotonic() + 0.25
    with pytest.raises(ValueError, match="시간이 초과"):
        repository.resolve("example.org")


def test_git_reads_only_regular_objects_at_selected_commit(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args]).decode().strip()

    git("init", "-q")
    with ZipFile(BytesIO(bot_package.export_zip(definition()))) as archive:
        archive.extractall(tmp_path / "sample")
    git("add", "sample")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "package")
    commit = git("rev-parse", "HEAD")
    git("update-index", "--cacheinfo", "120000," + git("rev-parse", "HEAD:sample/plugin.json") + ",sample/plugin.json")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "link")
    package = git_bot_source._Repository(tmp_path).read(commit, "sample")
    assert package.definition["id"] == "shared-research"
    with pytest.raises(ValueError, match="일반 파일"):
        git_bot_source._Repository(tmp_path).read(git("rev-parse", "HEAD"), "sample")

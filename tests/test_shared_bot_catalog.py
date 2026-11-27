from copy import deepcopy
from http.client import HTTPConnection
import json

import pytest

from agent_office import bot_package, git_bot_source, shared_bot_catalog
from test_studio_http import request, studio


URL = "https://example.org/public-bots.git"
COMMIT = "a" * 40
SKILL = "skills/inventory/SKILL.md"
DETAIL_URL = "https://example.org/"


def listing():
    return {
        "schemaVersion": 1,
        "entries": [{
            "id": "inventory-example", "botId": "stock-helper", "publisher": "Example maintainer",
            "name": "재고 도우미", "role": "Inventory researcher", "description": "Checks a public stock example.",
            "tags": ["inventory", "재고"], "package": {"url": URL, "commit": COMMIT, "path": "examples/stock"},
            "skills": [{"name": "Inventory", "path": SKILL, "url": DETAIL_URL}],
        }],
    }


def encoded(document):
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def test_validated_catalog_uses_normalized_public_git_and_opaque_local_links():
    document = listing()
    document["entries"][0]["package"]["url"] += "/"
    catalog = shared_bot_catalog.Catalog(lambda: encoded(document))
    result = catalog.refresh()
    assert result["schemaVersion"] == 1
    entry = result["entries"][0]
    assert entry["package"] == {"url": URL, "commit": COMMIT, "path": "examples/stock"}
    skill = entry["skills"][0]
    assert skill["name"] == "Inventory" and skill["path"] == SKILL and "url" not in skill
    prefix = "/bot-catalog/links/inventory-example/"
    assert skill["href"].startswith(prefix)
    assert len(skill["href"].removeprefix(prefix)) == 32
    assert catalog.link("inventory-example", skill["href"].removeprefix(prefix)) == DETAIL_URL


@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)", "http://example.org/", "https://user:pw@example.org/",
    "https://example.org/abc\r\nLocation: https://different.invalid/", "https:\\\\example.org\\path",
    "https://example.org/%0a",
])
def test_rejects_invalid_link_metadata(bad_url):
    document = listing()
    document["entries"][0]["skills"][0]["url"] = bad_url
    with pytest.raises(ValueError):
        shared_bot_catalog.loads(encoded(document))


def test_rejects_duplicate_entries_nonpinned_packages_and_duplicate_json_keys():
    document = listing()
    document["entries"].append(deepcopy(document["entries"][0]))
    with pytest.raises(ValueError):
        shared_bot_catalog.loads(encoded(document))
    document = listing()
    document["entries"][0]["package"]["commit"] = "v1"
    with pytest.raises(ValueError):
        shared_bot_catalog.loads(encoded(document))
    with pytest.raises(ValueError):
        shared_bot_catalog.loads(b'{"schemaVersion":1,"schemaVersion":1,"entries":[]}')


def test_workspace_catalog_reads_fresh_and_opens_only_a_registered_link(tmp_path):
    latest = listing()
    catalog = shared_bot_catalog.Catalog(lambda: encoded(latest))
    with studio(tmp_path / "catalog.db", accept_remote=True) as server:
        server.shared_bot_catalog = catalog
        workspace = server.catalog.create("catalog", [str(tmp_path)])["id"]
        base = f"/api/rooms/{workspace}/bot-catalog"
        assert request(server, base + "?office_journal=" + "0" * 32)[0] == 409
        assert request(server, "/api/peer/" + server.journal_id + base[4:])[0] == 404
        status, result = request(server, base + "?office_journal=" + server.journal_id)
        assert status == 200
        href = result["entries"][0]["skills"][0]["href"]
        connection = HTTPConnection(*server.server_address, timeout=10)
        try:
            connection.request("GET", href)
            response = connection.getresponse()
            assert response.status == 303
            assert response.getheader("Location") == DETAIL_URL
            assert response.getheader("Referrer-Policy") == "no-referrer"
            assert response.read() == b""
        finally:
            connection.close()
        assert request(server, href.replace("inventory-example", "unknown"))[0] == 404
        assert request(server, href + "?target=https://other.example/")[0] == 400

        latest["entries"][0]["package"]["commit"] = "b" * 40
        latest["entries"][0]["skills"][0]["url"] = "https://example.org/another-skill"
        status, refreshed = request(server, base)
        assert status == 200 and refreshed["entries"][0]["package"]["commit"] == "b" * 40
        assert request(server, href)[0] == 404

        latest["schemaVersion"] = 99
        assert request(server, base)[0] == 400
        assert refreshed["entries"][0]["skills"][0]["href"] != href


def test_registration_checks_bot_id_commit_and_declared_skill_against_git(monkeypatch):
    entries = shared_bot_catalog.loads(encoded(listing()))
    manifest = {
        "specVersion": "1.0", "id": "stock-helper", "name": "Inventory", "role": "Research",
        "files": {"SOUL.md": "Inventory.", SKILL: "Inventory skill."},
    }
    seen = []

    def fetch(source):
        seen.append(source)
        return git_bot_source.Fetched(COMMIT, bot_package.Package("fixture", None, manifest))

    monkeypatch.setattr(git_bot_source, "fetch", fetch)
    shared_bot_catalog.verify_packages(entries)
    assert seen[0].ref == COMMIT and seen[0].path == "examples/stock"
    del manifest["files"][SKILL]
    with pytest.raises(ValueError, match="SKILL.md"):
        shared_bot_catalog.verify_packages(entries)


def test_package_preview_reports_validated_bot_id(tmp_path, monkeypatch):
    manifest = {
        "specVersion": "1.0", "id": "stock-helper", "name": "Inventory", "role": "Research",
        "files": {"SOUL.md": "Inventory."},
    }
    monkeypatch.setattr(git_bot_source, "fetch", lambda _: git_bot_source.Fetched(
        COMMIT, bot_package.Package("fixture", None, manifest),
    ))
    with studio(tmp_path / "preview.db") as server:
        workspace = server.catalog.create("catalog", [str(tmp_path)])["id"]
        status, preview = request(server, f"/api/rooms/{workspace}/bots/package-preview",
                                  {"url": URL, "version": COMMIT})
        assert status == 200 and preview["bot_id"] == "stock-helper"

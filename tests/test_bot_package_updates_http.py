from copy import deepcopy
from uuid import uuid4

import pytest

from agent_office import bot_package, git_bot_source, local_bots
from agent_office.store import TaskStore
from test_studio_http import request, studio


URL = "https://example.org/bots.git"
BOT = "inventory-bot"
V1 = "1" * 40
V2 = "2" * 40
V3 = "3" * 40


def released(version):
    result = {
        "specVersion": "1.0", "id": BOT, "name": "Inventory", "role": "Research",
        "files": {"SOUL.md": f"{version} release instructions", "references/upstream.md": version},
        "folders": [], "extensions": {},
    }
    if version == "v2":
        result["files"]["references/added.md"] = "Only present in v2"
    return result


@pytest.fixture
def room(tmp_path, monkeypatch):
    releases = {version: (commit, released(version)) for version, commit in [("v1", V1), ("v2", V2), ("v3", V3)]}
    calls = []

    def fetch(source):
        if source.version not in releases:
            by_commit = {commit: pair for pair in releases.values() for commit in (pair[0],)}
            record = by_commit.get(source.version)
        else:
            record = releases[source.version]
        calls.append(source.version)
        if record is None:
            raise ValueError("remote unavailable")
        commit, document = record
        return git_bot_source.Fetched(commit, bot_package.Package(BOT, source.version, deepcopy(document)))

    monkeypatch.setattr(git_bot_source, "fetch", fetch)
    with studio(tmp_path / "studio.db", accept_remote=True) as server:
        workspace = server.catalog.create("recipient", [str(tmp_path)])["id"]
        other = server.catalog.create("other", [str(tmp_path)])["id"]
        catalog = f"/api/rooms/{workspace}/bots"
        source = {"url": URL, "version": "v1", "path": "sample"}
        status, preview = request(server, catalog + "/package-preview", source)
        assert status == 200
        status, receipt = request(server, catalog + "/package-install",
                                  {**source, "commit": V1, "digest": preview["digest"]})
        assert status == 200
        yield server, workspace, other, catalog, releases, calls


def save(server, member, filename, content):
    status, old = request(server, member + "/assets")
    assert status == 200
    status, new = request(server, member + "/assets", {
        "filename": filename, "content": content, "digest": old["digest"], "create": filename not in old["files"],
    })
    assert status == 200, new


def roster(server, catalog):
    return request(server, catalog)[1]


def test_update_and_offline_rollback_keep_local_edits_assignments_and_pinned_runs(room):
    server, workspace, other, catalog, releases, calls = room
    member = catalog + "/" + BOT
    save(server, member, "MEMORY.md", "personal note")
    save(server, member, "references/field.md", "local-only before update")
    assert request(server, catalog + "/assignment", {"id": BOT, "role": "developer"})[0] == 200
    run_id = uuid4().hex
    with TaskStore(server.db_path, existing=True) as store, store._write():
        local_bots.pin(store.connection, run_id, local_bots.resolve(store.connection, workspace))

    status, preview = request(server, member + "/package-update-preview", {"version": "v2"})
    assert status == 200
    assert preview["current"]["commit"] == V1 and preview["target"]["commit"] == V2
    assert preview["conflicts"] == [] and preview["token"]
    assert {"path": "references/field.md", "action": "kept"} in preview["changes"]
    assert {"path": "references/added.md", "action": "added"} in preview["changes"]
    save(server, member, "MEMORY.md", "private memo during preview")
    payload = {"version": "v2", "token": preview["token"]}
    status, receipt = request(server, member + "/package-update", payload)
    assert status == 200 and receipt["source"]["commit"] == V2 and receipt["rollback"]["commit"] == V1
    call_total = len(calls)
    assert request(server, member + "/package-update", payload) == (200, receipt)
    assert len(calls) == call_total
    assert request(server, member + "/package-update-preview", {"version": "v2"})[1]["unchanged"] is True
    assert request(server, member.replace(workspace, other) + "/package-rollback-preview", {})[0] == 404
    assert request(server, "/api/peer/" + server.journal_id + member[4:] + "/package-rollback-preview", {})[0] == 404
    assert request(server, member + "/package-rollback-preview", {}, Origin="https://other.invalid")[0] == 403
    asset = request(server, member + "/assets")[1]
    assert asset["files"]["SOUL.md"] == "v2 release instructions"
    assert asset["files"]["MEMORY.md"] == "private memo during preview"
    assert request(server, catalog)[1]["assignments"]["developer"] == BOT
    assert next(bot for bot in roster(server, catalog)["bots"] if bot["id"] == BOT)["package_rollback"]["commit"] == V1

    save(server, member, "references/after.md", "local-only after update")
    save(server, member, "SOUL.md", "new local edit on the same file")
    releases.clear()
    call_total = len(calls)
    status, blocked = request(server, member + "/package-rollback-preview", {})
    assert status == 200 and blocked["conflicts"] == ["SOUL.md"] and blocked["token"] is None
    save(server, member, "SOUL.md", "v2 release instructions")
    status, previous = request(server, member + "/package-rollback-preview", {})
    assert status == 200 and previous["target"]["commit"] == V1
    body = {"token": previous["token"]}
    status, rolled_back = request(server, member + "/package-rollback", body)
    assert status == 200 and rolled_back["source"]["commit"] == V1 and rolled_back["rollback"] is None
    assert request(server, member + "/package-rollback", body) == (200, rolled_back)
    assert request(server, member + "/package-rollback-preview", {})[0] == 404
    assert len(calls) == call_total
    assert request(server, member + "/assets")[1]["files"] == {
        **released("v1")["files"], "MEMORY.md": "private memo during preview",
        "references/field.md": "local-only before update", "references/after.md": "local-only after update",
    }
    with TaskStore(server.db_path, existing=True, readonly=True) as store:
        assert local_bots.for_run(store.connection, run_id)["developer"]["instructions"] == "v1 release instructions"


def test_update_preview_rejects_new_local_edits_and_moved_tags_without_consuming_backup(room):
    server, workspace, other, catalog, releases, _ = room
    member = catalog + "/" + BOT
    route = member + "/package-update-preview"
    save(server, member, "SOUL.md", "local conflict")
    status, conflict = request(server, route, {"version": "v2"})
    assert status == 200 and conflict["conflicts"] == ["SOUL.md"] and conflict["token"] is None
    save(server, member, "SOUL.md", "v1 release instructions")
    status, preview = request(server, route, {"version": "v2"})
    assert status == 200
    save(server, member, "references/field.md", "concurrent edit")
    assert request(server, member + "/package-update", {"version": "v2", "token": preview["token"]})[0] == 409
    status, preview = request(server, route, {"version": "v2"})
    assert status == 200
    releases["v2"] = releases["v3"]
    assert request(server, member + "/package-update", {"version": "v2", "token": preview["token"]})[0] == 409
    assert request(server, member + "/package-rollback-preview", {})[0] == 404
    assert request(server, member + "/assets")[1]["files"]["SOUL.md"] == "v1 release instructions"


def test_legacy_install_requires_original_commit_only_when_local_definition_differs(room):
    server, workspace, other, catalog, releases, calls = room
    member = catalog + "/" + BOT
    with TaskStore(server.db_path, existing=True) as store, store._write():
        store.connection.execute("DROP TABLE studio_bot_package_versions")
    save(server, member, "MEMORY.md", "memo-only difference")
    calls.clear()
    assert request(server, member + "/package-update-preview", {"version": "v2"})[0] == 200
    assert calls == ["v2"]
    save(server, member, "references/local.md", "legacy local edit")
    calls.clear()
    status, result = request(server, member + "/package-update-preview", {"version": "v2"})
    assert status == 200 and result["token"]
    assert set(calls) == {V1, "v2"}
    del releases["v1"]
    status, error = request(server, member + "/package-update-preview", {"version": "v2"})
    assert status == 400 and "기존" in error["error"]["message"]
    assert request(server, member + "/assets")[1]["files"]["references/local.md"] == "legacy local edit"

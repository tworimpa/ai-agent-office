from io import BytesIO
from zipfile import ZipFile

from agent_office import bot_package, git_bot_source
from test_bot_package import definition
from test_studio_http import project, request, studio


def test_git_package_preview_install_and_provenance_are_workspace_local(tmp_path, monkeypatch):
    folder, _ = project(tmp_path)
    document = definition()
    with ZipFile(BytesIO(bot_package.export_zip(document))) as archive:
        package = bot_package.read(archive.read("plugin.json"), archive.read(bot_package.DEFINITION_PATH), {})
    commits = iter(["a" * 40, "b" * 40, "a" * 40, "a" * 40])
    sources = []

    def fetch(source):
        sources.append(source)
        return git_bot_source.Fetched(next(commits), package)

    monkeypatch.setattr(git_bot_source, "fetch", fetch)
    db = tmp_path / "studio.db"
    with studio(db) as server:
        def workspace(name):
            return server.catalog.create(name, [str(folder)])["id"]

        author, recipient = workspace("author"), workspace("recipient")
        author_base = f"/api/rooms/{author}/bots"
        base = f"/api/rooms/{recipient}/bots"
        assert request(server, author_base + "/import", document)[0] == 200
        status, archive_bytes = request(server, author_base + "/shared-research/package")
        assert status == 200
        with ZipFile(BytesIO(archive_bytes)) as archive:
            assert b"MEMORY.md" not in archive.read(bot_package.DEFINITION_PATH)
        assert "MEMORY.md" in request(server, author_base + "/shared-research/manifest")[1]["files"]

        source = {"url": "https://example.org/bots.git", "version": "v1", "path": "sample"}
        assert request(server, base + "/package-preview", source, Origin="https://other.invalid")[0] == 403
        assert request(server, base + "/package-preview?office_journal=" + "0" * 32, source)[0] == 409
        status, preview = request(server, base + "/package-preview", source)
        assert status == 200 and preview["source"]["commit"] == "a" * 40
        assert "references/source.md" in preview["files"] and "MEMORY.md" not in preview["files"]
        body = {**source, "commit": preview["source"]["commit"], "digest": preview["digest"]}
        assert request(server, base + "/package-install", body)[0] == 409
        assert not any(bot["id"] == "shared-research" for bot in request(server, base)[1]["bots"])
        for _ in range(2):
            status, receipt = request(server, base + "/package-install", body)
            assert status == 200 and receipt["source"] == preview["source"]
        status, assets = request(server, base + "/shared-research/assets")
        assert status == 200 and assets["files"]["MEMORY.md"] == ""
        assert request(server, base)[1]["assignments"]["developer"] == "office-developer"
        assert [item.ref for item in sources] == ["refs/tags/v1"] * 4

    with studio(db) as restarted:
        bots = request(restarted, base)[1]["bots"]
        shared = next(bot for bot in bots if bot["id"] == "shared-research")
        assert shared["package"] == preview["source"]

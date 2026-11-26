import base64
from copy import deepcopy
from importlib import resources
import json
from uuid import uuid4

from jsonschema import Draft202012Validator
import pytest

from agent_office import bot_spec, cli, local_bots as bots
from agent_office.goal_workers import RoomWorkers
from agent_office.models import OfficeError
from agent_office.store import TaskStore
from agent_office.studio_catalog import StudioCatalog


PACKAGE = resources.files("agent_office")
EXAMPLE = PACKAGE.joinpath("examples/research.bot.json")
SCHEMA = json.loads(PACKAGE.joinpath("schemas/bot-1.0.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


def example():
    return json.loads(EXAMPLE.read_bytes())


def test_versioned_client_contract_and_example():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == bot_spec.SCHEMA_ID
    VALIDATOR.validate(example())
    assert bot_spec.loads(EXAMPLE.read_bytes()) == example()
    portable = {k: v for k, v in example().items() if k not in ("folders", "extensions", "$schema")}
    VALIDATOR.validate(portable)
    assert bot_spec.validate(portable)["extensions"] == {}


@pytest.mark.parametrize("changes", [
    {"specVersion": "2.0"}, {"id": "Uppercase"}, {"role": ""}, {"name": " 다온 "}, {"engine": "codex"},
    {"files": {"AGENTS.md": "x"}}, {"folders": ["a", "a"]}, {"files": {"SOUL.md": "x", "../x": ""}},
    {"extensions": {"plain": {}}}, {"extensions": {bot_spec.APPEARANCE: {"preset": True}}},
    {"extensions": {bot_spec.APPEARANCE: {"preset": 12}}}, {"extensions": {bot_spec.APPEARANCE: {"preset": 1, "url": "x"}}},
])
def test_schema_and_adapter_reject_invalid_structure(changes):
    package = {**example(), **changes}
    assert not VALIDATOR.is_valid(package)
    with pytest.raises(ValueError):
        bot_spec.validate(package)


@pytest.mark.parametrize("bad", [
    "C:/text.md", "//remote/file", "../outside", "a/../b", "a//b", "a/", "a\\b", "a/b.", "a/b ",
    "a/CoN.txt", "LPT¹", ".git/config", "data:stream", "\u202efile", "e\u0301.txt", "memory.md", "MEMORY.md/child",
    "/".join(["x"] * 9), "가" * 43, "/".join(["x" * 61] * 4),
])
def test_paths_rejected_across_hosts(bad):
    package = example()
    package["files"][bad] = "text"
    with pytest.raises(ValueError):
        bot_spec.validate(package)


def test_tree_collisions_and_limits():
    for files, folders in [
        ({"skills/q": "a", "Skills/z": ""}, []), ({"a": "", "a/b": "b"}, []),
        ({"x/y": ""}, ["x/Y"]), ({}, ["A/b", "a"]), ({}, ["MEMORY.md"]),
        ({"a": "가" * 2731}, []), ({str(i): "a" * 8192 for i in range(4)}, []),
        ({str(i): "" for i in range(32)}, []), ({}, [str(i) for i in range(33)]),
    ]:
        with pytest.raises(ValueError):
            bot_spec.validate({**example(), "files": {"SOUL.md": "x", **files}, "folders": folders})
    with pytest.raises(ValueError):
        bot_spec.loads(b'{"extensions":{"org.example":1,"org.example":2}}')
    with pytest.raises(ValueError):
        bot_spec.loads(b" " * (bot_spec.MAX_DOCUMENT_BYTES + 1))
    nested = {"a": []}
    for _ in range(17):
        nested = [nested]
    for value in [float("nan"), nested, "a" * (160 * 1024)]:
        with pytest.raises(ValueError):
            bot_spec.validate({**example(), "extensions": {"org.example": value}})


def test_appearance_is_a_bounded_static_webp():
    # A real 1x1 lossy WebP. Verify RIFF size, format, dimensions and complete chunk boundaries.
    webp = base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")
    prefix = "data:image/webp;base64,"
    image = prefix + base64.b64encode(webp).decode()
    appearance = {"preset": 11, "image": image}
    document = {**example(), "extensions": {bot_spec.APPEARANCE: appearance}}
    VALIDATOR.validate(document)
    assert bot_spec.validate(document)["extensions"][bot_spec.APPEARANCE] == appearance
    too_wide = bytearray(webp)
    too_wide[26:28] = (513).to_bytes(2, "little")
    for invalid in [
        "https://example.org/avatar.webp", "data:image/svg+xml;base64,PHN2Zy8+", prefix + "####",
        prefix + base64.b64encode(b"<svg/>").decode(), prefix + base64.b64encode(webp[:-1]).decode(),
        prefix + base64.b64encode(too_wide).decode(), prefix + "a" * (131072 + 1),
    ]:
        with pytest.raises(ValueError):
            bot_spec.validate({**document, "extensions": {bot_spec.APPEARANCE: {"preset": 0, "image": invalid}}})


def test_install_export_edit_and_pin(tmp_path):
    workspace, run = uuid4().hex, uuid4().hex
    with TaskStore(tmp_path / "office.db") as store, store._write():
        db = store.connection
        bots.ensure_schema(db)
        package = example()
        original = bots.install(db, workspace, package)
        bot_id = original["id"]
        assert bots.install(db, workspace, package) == original
        assert bots.manifest(db, workspace, bot_id) == package
        assert bots.tree(original)["folders"] == ["hooks", "mcp", "notes", "notes/interviews", "skills", "skills/interview"]
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            bots.install(db, workspace, {**package, "name": "다른 봇"})
        bots.assign(db, workspace, "developer", bot_id)
        bots.pin(db, run, bots.resolve(db, workspace))
        new = bots.save_entry(db, workspace, bot_id, "data/logs", "", original["digest"], kind="folder", create=True)
        assert "data/logs" in bots.tree(new)["folders"]
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            bots.save_entry(db, workspace, bot_id, "SOUL.md", "x", original["digest"])
        for path in ["data", "Data/reports.txt", "data/logs", "SOUL.md"]:
            with pytest.raises(ValueError):
                bots.save_entry(db, workspace, bot_id, path, "x", new["digest"], create=True)
        changed = bots.save_entry(db, workspace, bot_id, "data/logs/new.txt", "new", new["digest"], create=True)
        extensions = deepcopy(package["extensions"])
        extensions[bot_spec.APPEARANCE] = {"preset": 7}
        changed = bots.set_extensions(db, workspace, bot_id, extensions, changed["digest"])
        changed = bots.save_asset(db, workspace, bot_id, "MEMORY.md", "메모", changed["digest"])
        assert changed["extensions"] == extensions
        assert changed["assets"]["data/logs/new.txt"] == "new"
        assert bots.for_run(db, run)["developer"] == original
        assert not list(tmp_path.rglob("new.txt"))


def test_pinned_prompt_uses_text_not_presentation_or_unknown_extensions(tmp_path):
    workspace, run = uuid4().hex, uuid4().hex
    journal = tmp_path / "office.db"
    with TaskStore(journal) as store, store._write():
        db = store.connection
        bots.ensure_schema(db)
        package = example()
        package["extensions"]["org.example.private"] = "PRIVATE_EXTENSION_SENTINEL"
        assigned = bots.install(db, workspace, package)
        bots.assign(db, workspace, "developer", assigned["id"])
        snapshot = bots.resolve(db, workspace)
        bots.pin(db, run, snapshot)
    workers = RoomWorkers(journal, run, {}, {"bots": bots.public_receipt(snapshot)}, {})
    prompt = workers.bot_prompt("developer", "Task\nInspect notes.")
    assert "skills/interview/SKILL.md" in prompt and package["files"]["SOUL.md"] in prompt
    for string in [bot_spec.APPEARANCE, "PRIVATE_EXTENSION_SENTINEL", "notes/interviews",
                   "mcp/servers.json", "hooks/events.json"]:
        assert string not in prompt
    assert "skills/interview/SKILL.md" not in workers.bot_prompt("reviewer", "Review")


@pytest.mark.parametrize("role,asset,sha", [
    ("developer", None, "91f375e42f46cc0db2ddc1836b50ff0c59bcc84981b29a419a9ab70fe0f92fd3"),
    ("리서처", "질문.md", "0a6df4566b2e8008f03bae324fdeb4136cb1d2fc88fef96a89b402bbb171192c"),
    ("리서처", "MEMORY.md", "ed9dd3a7c1f8c8b0747339d64dec242ddd42c7c7a192115cff211d02659ef238"),
])
def test_old_storage_digest_survives_portable_roundtrip(tmp_path, role, asset, sha):
    workspace, other = uuid4().hex, uuid4().hex
    with TaskStore(tmp_path / "office.db") as store, store._write():
        db = store.connection
        bots.ensure_schema(db)
        bot = bots.create(db, workspace, "research", "다온", role, "관찰을 기록한다.")
        if asset:
            bot = bots.save_asset(db, workspace, bot["id"], asset, "", bot["digest"])
        assert bot["digest"] == sha
        manifest = bots.manifest(db, workspace, bot["id"])
        assert ("MEMORY.md" in manifest["files"]) == (asset == "MEMORY.md")
        assert bots.install(db, other, manifest) == bot
        builtin = bots.get(db, workspace, "office-reviewer")
        assert bots.install(db, other, bots.manifest(db, workspace, builtin["id"])) == builtin


def test_portable_cli_offline_validation_import_and_export(tmp_path, capsys):
    db = tmp_path / "tasks.db"
    source = tmp_path / "원본.json"
    source.write_bytes(EXAMPLE.read_bytes())

    def invoke(*args):
        status = cli.main(["--db", str(db), "bots", *args])
        result = capsys.readouterr()
        return status, json.loads(result.out if status == 0 else result.err)

    assert invoke("validate", "--file", str(source))[1]["valid"]
    assert not db.exists()
    with TaskStore(db):
        pass
    workspace = StudioCatalog(db).create("Office", [str(tmp_path)])["id"]
    scope = ["--workspace-id", workspace]
    assert invoke("import", *scope, "--file", str(source))[0] == 0
    exporting = ("export", *scope, "--id", "ux-researcher")
    assert invoke(*exporting) == (0, example())
    output = tmp_path / "사본.json"
    assert invoke(*exporting, "--output", str(output))[0] == 0
    assert bot_spec.loads(output.read_bytes()) == example()
    assert invoke(*exporting, "--output", str(output))[1]["error"]["code"] == "FILE_IO"
    source.write_text(json.dumps({**example(), "role": "변경"}), encoding="utf-8")
    assert invoke("import", *scope, "--file", str(source))[1]["error"]["code"] == "REQUEST_CONFLICT"

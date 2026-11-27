"""Local HTTP bindings for the bot specification, catalog and execution service."""
import base64
from contextlib import nullcontext
from importlib.resources import files
import re

from . import bot_package, bot_package_updates, bot_runtime, bot_spec, git_bot_source, local_bots
from .models import OfficeError
from .store import TaskStore

APPEARANCE = bot_spec.APPEARANCE
ROOM = r"/api/rooms/([0-9a-f]{32})"
BOT = r"([a-z0-9]+(?:-[a-z0-9]+)*)"
SCHEMA = "/api/bot-spec/1.0/schema"
EXAMPLE = "/api/bot-spec/1.0/example"


def catalog_view(db, workspace, journal):
    result = []
    sources = bot_package.installed_sources(db, workspace)
    rollbacks = bot_package_updates.rollback_sources(db, workspace)
    for bot in local_bots.list_bots(db, workspace):
        public = {k: bot[k] for k in ("id", "name", "role", "instructions", "digest", "builtin", "schema_version")}
        appearance = local_bots.tree(bot)["extensions"].get(APPEARANCE, {})
        public["appearance"] = {"preset": appearance.get("preset", 0)}
        if appearance.get("image"):
            public["appearance"]["image"] = f"/api/rooms/{workspace}/bots/{bot['id']}/portrait?office_journal={journal}"
        if bot["id"] in sources:
            public["package"] = sources[bot["id"]]
            if bot["id"] in rollbacks:
                public["package_rollback"] = rollbacks[bot["id"]]
        result.append(public)
    return {"bots": result, "assignments": {slot: b["id"] for slot, b in local_bots.resolve(db, workspace).items()}}


def _git_package(server, workspace, action, data):
    request = data.copy()
    expected = None
    if action == "package-install":
        expected = (request.pop("commit", None), request.pop("digest", None))
        if not all(isinstance(value, str) for value in expected):
            raise ValueError("먼저 설치할 패키지를 확인하세요.")
    source = git_bot_source.Source.parse(request)
    fetched = git_bot_source.fetch(source)
    package = fetched.package
    document = package.definition
    digest = local_bots.shareable_digest(document)
    receipt = source.receipt(fetched.commit, digest)
    if expected is None:
        return 200, {"source": receipt, "digest": digest, "bot_id": document["id"], "package_name": package.name,
                     "package_version": package.version, "name": document["name"], "role": document["role"],
                     "files": sorted(document["files"])}
    if expected != (fetched.commit, digest):
        raise OfficeError("REQUEST_CONFLICT")
    bot_id = document["id"]
    with TaskStore(server.db_path, existing=True) as store, store._write():
        db = store.connection
        local_bots.ensure_schema(db)
        previous = bot_package.record_source(db, workspace, bot_id, receipt)
        if previous is None:
            local_bots.install(db, workspace, document)
        bot_package_updates.record_initial(db, workspace, bot_id, document)
        return 200, {"id": bot_id, "source": previous or receipt}


def handle(server, path, data):
    """Return a response tuple for local routes, or None for other Studio routes."""
    if path in (SCHEMA, EXAMPLE) and data is None:
        name = "schemas/bot-1.0.schema.json" if path == SCHEMA else "examples/research.bot.json"
        return 200, files("agent_office").joinpath(name).read_bytes(), "application/json"
    if path == "/api/bot-spec" and data is None:
        return 200, {"specVersion": "1.0", "schema": SCHEMA, "example": EXAMPLE}
    if data is None:
        if match := re.fullmatch(ROOM + "/bot-catalog", path):
            server.catalog.get(match[1])
            return 200, server.shared_bot_catalog.refresh()
        if match := re.fullmatch(ROOM + r"/bots/" + BOT + r"/chat/before/([0-9a-f]{32})", path):
            return 200, bot_runtime.chat(server.rooms, match[1], match[2], before=match[3])
        if match := re.fullmatch(ROOM + r"/bot-orders/before/([0-9a-f]{32})", path):
            return 200, bot_runtime.orders(server.rooms, match[1], before=match[2])
    if match := re.fullmatch(ROOM + "/bot-orders", path):
        workspace = match[1]
        server.catalog.get(workspace)
        if data is None:
            return 200, bot_runtime.orders(server.rooms, workspace)
        return 202, bot_runtime.submit_order(server.rooms, workspace, data)
    if not path.startswith("/api/rooms/") or "/bots" not in path:
        return None
    change = re.fullmatch(ROOM + "/bots/" + BOT + r"/package-(update|rollback)(-preview)?", path)
    if change:
        if data is None:
            return None
        workspace, bot_id, action, preview = change.groups()
        server.catalog.get(workspace)
        return 200, bot_package_updates.change(server.db_path, workspace, bot_id, action, data, preview=bool(preview))
    catalog = re.fullmatch(ROOM + r"/bots(?:/(assignment|import|package-preview|package-install))?", path)
    member = re.fullmatch(ROOM + "/bots/" + BOT + r"/(assets|appearance|portrait|manifest|package|chat|commands)", path)
    if not (catalog or member):
        return None
    workspace = (catalog or member)[1]
    server.catalog.get(workspace)
    write = data is not None
    if catalog and catalog[2] in {"package-preview", "package-install"}:
        return _git_package(server, workspace, catalog[2], data) if write else None
    if member:
        bot_id, action = member[2], member[3]
        if action == "commands" and write:
            return 202, {"run": server.rooms.assign(workspace, data, target_bot=bot_id)}
        if action == "chat" and not write:
            return 200, bot_runtime.chat(server.rooms, workspace, bot_id)

    with TaskStore(server.db_path, existing=True, readonly=not write) as store, (
            store._write() if write else nullcontext()):
        db = store.connection
        if write:
            local_bots.ensure_schema(db)
        else:
            db.execute("BEGIN")
        if catalog:
            if not write and catalog[2]:
                return None
            if write:
                if catalog[2] == "import":
                    bot = local_bots.install(db, workspace, data)
                    return 200, {"bot": local_bots.manifest(db, workspace, bot["id"]), "digest": bot["digest"]}
                if catalog[2] == "assignment":
                    if set(data) != {"id", "role"}:
                        raise ValueError("배정할 자리와 봇 ID를 입력하세요.")
                    local_bots.assign(db, workspace, data["role"], data["id"])
                else:
                    if set(data) - {"appearance"} != {"id", "name", "role", "instructions"}:
                        raise ValueError("봇의 ID, 이름, 역할과 지침을 입력하세요.")
                    if "appearance" in data:
                        manifest = {"specVersion": "1.0", "id": data["id"], "name": data["name"], "role": data["role"],
                                    "files": {"SOUL.md": data["instructions"]}, "folders": [],
                                    "extensions": {APPEARANCE: data["appearance"]}}
                        local_bots.install(db, workspace, manifest)
                    else:
                        local_bots.create(db, workspace, data["id"], data["name"], data["role"], data["instructions"])
            return 200, catalog_view(db, workspace, server.journal_id)
        if action == "manifest" and not write:
            return 200, local_bots.manifest(db, workspace, bot_id)
        if action == "package" and not write:
            document = local_bots.manifest(db, workspace, bot_id)
            local_bots.shareable_digest(document)
            return 200, bot_package.export_zip(document), "application/zip"
        if action == "assets" and write:
            if (not {"filename", "digest"} <= data.keys()
                    or data.keys() - {"filename", "content", "digest", "kind", "create"}
                    or type(data.get("create", False)) is not bool
                    or (data.get("kind", "file") == "file" and "content" not in data)):
                raise ValueError("파일 경로, 내용과 편집을 시작한 버전을 확인하세요.")
            filename = data["filename"]
            if (set(data) == {"filename", "content", "digest"} and isinstance(filename, str)
                    and "/" not in filename and filename.endswith((".md", ".txt"))):
                bot = local_bots.save_asset(db, workspace, bot_id, filename, data["content"], data["digest"])
            else:
                bot = local_bots.save_entry(db, workspace, bot_id, filename, data.get("content", ""),
                                            data["digest"], kind=data.get("kind", "file"),
                                            create=data.get("create", False))
            return 200, local_bots.tree(bot)
        bot = local_bots.get(db, workspace, bot_id)
        tree = local_bots.tree(bot)
        if action == "assets" and not write:
            return 200, tree
        if action == "appearance" and write:
            if set(data) != {"appearance", "digest"}:
                raise ValueError("캐릭터와 편집을 시작한 버전을 확인하세요.")
            extensions = {**tree["extensions"], APPEARANCE: data["appearance"]}
            updated = local_bots.set_extensions(db, workspace, bot_id, extensions, data["digest"])
            return 200, local_bots.tree(updated)
        if action == "portrait" and not write:
            image = tree["extensions"].get(APPEARANCE, {}).get("image")
            if not image:
                raise OfficeError("NOT_FOUND")
            return 200, base64.b64decode(image.partition(",")[2], validate=True), "image/webp"
    return None

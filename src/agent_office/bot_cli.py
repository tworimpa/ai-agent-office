"""Manage local role bots for a registered Studio workspace."""

import json
from pathlib import Path
import sys

from . import bot_spec, local_bots
from .models import OfficeError
from .store import TaskStore
from .studio_catalog import StudioCatalog


def add_commands(sub):
    bots = sub.add_parser("bots", help="Manage local role bots for Workspace assignments")
    actions = bots.add_subparsers(dest="bot_command", required=True)
    actions.add_parser("workspaces", help="List registered Workspace IDs and names")
    create = actions.add_parser("create", help="Create a local bot with a custom role")
    assign = actions.add_parser("assign", help="Bind a bot to a role for future assignments")
    listing = actions.add_parser("list", help="List bot definitions and current role bindings")
    show = actions.add_parser("show", help="Read one bot definition and its instructions")
    validate = actions.add_parser("validate", help="Validate a portable bot JSON file offline")
    importing = actions.add_parser("import", help="Install a portable bot in a Workspace")
    exporting = actions.add_parser("export", help="Write a portable bot definition as UTF-8 JSON")
    for action in (validate, importing):
        action.add_argument("--file", required=True, type=Path, help="Path to the UTF-8 JSON definition")
    exporting.add_argument("--output", type=Path, help="New file to write (default: stdout)")
    for action in (create, assign, listing, show, importing, exporting):
        action.add_argument("--workspace-id", required=True, help="ID from bots workspaces")
    for action in (create, assign, show, exporting):
        action.add_argument("--id", dest="bot_id", required=True, help="Local bot ID")
    for action in (create, assign):
        action.add_argument("--role", required=True, choices=local_bots.ROLES if action is assign else None)
    create.add_argument("--name", required=True, help="Display name")
    create.add_argument("--instructions", required=True, help="Plain-text instructions for this role")


def _result(args, db, workspace_id):
    if args.bot_command == "export":
        return local_bots.manifest(db, workspace_id, args.bot_id)
    if args.bot_command == "import":
        return {"bot": local_bots.install(db, workspace_id, args.manifest)}
    if args.bot_command == "create":
        return {"bot": local_bots.create(db, workspace_id, args.bot_id, args.name, args.role, args.instructions)}
    if args.bot_command == "assign":
        return {"role": args.role, "bot": local_bots.assign(db, workspace_id, args.role, args.bot_id)}
    if args.bot_command == "show":
        return {"bot": local_bots.get(db, workspace_id, args.bot_id)}
    return {
        "bots": [{key: bot[key] for key in ("id", "name", "role", "digest", "builtin", "schema_version")}
                 for bot in local_bots.list_bots(db, workspace_id)],
        "bindings": {role: bot["id"] for role, bot in local_bots.resolve(db, workspace_id).items()},
    }


def _write_json(stream, value, *, indent=None):
    text = json.dumps(value, ensure_ascii=False, indent=indent) + "\n"
    if hasattr(stream, "buffer"):
        stream.buffer.write(text.encode("utf-8"))
    else:
        stream.write(text)


def run_command(args, db_path):
    try:
        if args.bot_command in ("validate", "import"):
            with args.file.open("rb") as stream:
                args.manifest = bot_spec.loads(stream.read(bot_spec.MAX_DOCUMENT_BYTES + 1))
            if args.bot_command == "validate":
                print(json.dumps({"valid": True, "id": args.manifest["id"], "specVersion": bot_spec.VERSION}))
                return 0
        editing = args.bot_command in ("create", "assign", "import")
        with TaskStore(db_path, existing=True, readonly=not editing) as store:
            catalog = StudioCatalog(db_path)
            if args.bot_command == "workspaces":
                result = {"workspaces": [{"id": item["id"], "name": item["name"]} for item in catalog.list()]}
            else:
                workspace_id = catalog.get(args.workspace_id)["id"]
                if editing:
                    with store._write():
                        local_bots.ensure_schema(store.connection)
                        result = _result(args, store.connection, workspace_id)
                else:
                    store.connection.execute("BEGIN")
                    result = _result(args, store.connection, workspace_id)
        if args.bot_command == "export" and args.output:
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(result, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
            result = {"id": result["id"], "path": str(args.output)}
    except (OfficeError, OSError, ValueError) as exc:
        if isinstance(exc, OfficeError):
            if exc.code == "NOT_FOUND":
                code, message = exc.code, "Workspace or bot does not exist."
            elif exc.code == "REQUEST_CONFLICT":
                code, message = exc.code, "Another definition already uses this bot ID."
            else:
                raise
        elif isinstance(exc, OSError):
            code, message = "FILE_IO", str(exc)
        else:
            code, message = "INVALID_ARGUMENT", str(exc)
        _write_json(sys.stderr, {"error": {"code": code, "message": message}})
        return 2
    _write_json(sys.stdout, result, indent=2)
    return 0

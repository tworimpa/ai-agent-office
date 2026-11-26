"""Manage local role bots for a registered Studio workspace."""

import json
import sys

from . import local_bots
from .models import OfficeError
from .store import TaskStore
from .studio_catalog import StudioCatalog


def add_commands(sub):
    bots = sub.add_parser("bots", help="Manage local role bots for Workspace assignments")
    actions = bots.add_subparsers(dest="bot_command", required=True)
    actions.add_parser("workspaces", help="List registered Workspace IDs and names")
    create = actions.add_parser("create", help="Create an immutable bot definition")
    assign = actions.add_parser("assign", help="Bind a bot to a role for future assignments")
    listing = actions.add_parser("list", help="List bot definitions and current role bindings")
    show = actions.add_parser("show", help="Read one bot definition and its instructions")
    for action in (create, assign, listing, show):
        action.add_argument("--workspace-id", required=True, help="ID from bots workspaces")
    for action in (create, assign, show):
        action.add_argument("--id", dest="bot_id", required=True, help="Local bot ID")
    for action in (create, assign):
        action.add_argument("--role", required=True, choices=local_bots.ROLES)
    create.add_argument("--name", required=True, help="Display name")
    create.add_argument("--instructions", required=True, help="Plain-text instructions for this role")


def _result(args, db, workspace_id):
    if args.bot_command == "create":
        return {"bot": local_bots.create(db, workspace_id, args.bot_id, args.name, args.role, args.instructions)}
    if args.bot_command == "assign":
        return {"role": args.role, "bot": local_bots.assign(db, workspace_id, args.role, args.bot_id)}
    if args.bot_command == "show":
        return {"bot": local_bots.get(db, workspace_id, args.bot_id)}
    return {
        "bots": [{key: value for key, value in bot.items() if key != "instructions"}
                 for bot in local_bots.list_bots(db, workspace_id)],
        "bindings": {role: bot["id"] for role, bot in local_bots.resolve(db, workspace_id).items()},
    }


def run_command(args, db_path):
    try:
        editing = args.bot_command in ("create", "assign")
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
    except (OfficeError, ValueError) as exc:
        if isinstance(exc, OfficeError):
            if exc.code != "NOT_FOUND":
                raise
            code, message = exc.code, "Workspace or bot does not exist."
        else:
            code, message = "INVALID_ARGUMENT", str(exc)
        print(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0

"""A user-owned Studio that stays alive independently of the desktop window."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .models import OfficeError
from .ownership import RunGuard
from .studio_server import StudioHandler, StudioServer
from .studio_ssh import RemoteHost


class DesktopHandler(StudioHandler):
    def _local_api(self, path, data):
        if path == "/api/desktop-runtime" and self.command in ("GET", "HEAD"):
            self._reply(200, self.server.desktop_receipt)
        else:
            super()._local_api(path, data)


def serve(directory: Path, remotes: tuple[RemoteHost, ...], expected_journal=None):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = directory / "runtime.json"
    boot_id = uuid4().hex
    stop_path = directory / f"stop-{boot_id}"
    pending = directory / f".runtime-{boot_id}.tmp"
    db_path = directory / "tasks.sqlite3"
    with RunGuard.desktop(db_path):
        try:
            with StudioServer(db_path, 0, remotes=remotes, expected_journal=expected_journal, standalone=True) as server:
                server.RequestHandlerClass = DesktopHandler
                server.desktop_receipt = {
                    "boot_id": boot_id,
                    "journal_id": server.journal_id,
                    "port": server.server_address[1],
                    "remotes": [
                        {"alias": host.alias, "port": host.port, "journalId": host.journal_id}
                        for host in remotes
                    ],
                }
                with pending.open("x", encoding="utf-8") as stream:
                    json.dump(server.desktop_receipt, stream)
                pending.replace(receipt_path)
                server.timeout = 0.25
                while not stop_path.is_file():
                    server.handle_request()
        finally:
            pending.unlink(missing_ok=True)
        receipt_path.unlink(missing_ok=True)
    stop_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a separate local desktop Studio")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--expected-journal")
    parser.add_argument("--remote", action="append", nargs=3, default=[], metavar=("ALIAS", "PORT", "JOURNAL_ID"))
    args = parser.parse_args(argv)
    if not args.data_dir.is_absolute():
        parser.error("Provide an absolute desktop data path")
    os.environ["AGENT_OFFICE_DESKTOP_LOCAL"] = "1"
    try:
        remotes = tuple(RemoteHost(alias, int(port), journal) for alias, port, journal in args.remote)
        serve(args.data_dir, remotes, args.expected_journal)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, OfficeError, sqlite3.Error) as exc:
        parser.exit(1, f"Local Studio could not start ({type(exc).__name__}).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

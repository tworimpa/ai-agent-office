"""Service-account entry point. Only this process imports the selected Office source."""
import argparse
from importlib import import_module
from importlib.machinery import PathFinder, SourceFileLoader
import os
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from . import deployment, release_view, runtime_ops, runtime_permissions


class _SelectedOffice:
    """A missing Office module must fail, including in editable installations."""

    def __init__(self, source):
        self.source = source

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "agent_office" and not fullname.startswith("agent_office."):
            return None
        parent = self.source.joinpath(*fullname.split(".")[:-1])
        spec = PathFinder.find_spec(fullname, [str(parent)])
        if spec and spec.loader is None and spec.submodule_search_locations is not None:
            if list(map(Path, spec.submodule_search_locations)) == [parent / fullname.rsplit(".", 1)[-1]]:
                return spec  # A single selected namespace directory is safe.
        if (spec is None or not isinstance(spec.loader, SourceFileLoader)
                or not Path(spec.origin).is_relative_to(self.source)):
            raise ModuleNotFoundError(f"Selected release is missing Python source for {fullname}")
        return spec


def _load_slot(slot):
    source = slot / "src"
    package = source / "agent_office"
    if not (package / "__init__.py").is_file():
        raise ValueError("Release has no compatible Office source package")
    # Owned Python subprocesses also use this import root. No startup hooks,
    # foreign top-level modules or cached/native replacements can precede it.
    if any(entry != package for entry in source.iterdir()):
        raise ValueError("Unexpected package or module in release src directory")
    if any(p.name.casefold() == "__pycache__" or p.suffix.casefold() in {".pyc", ".pyo", ".pyd", ".so"}
           for p in package.rglob("*")):
        raise ValueError("Managed releases require Python source modules")
    os.environ["PYTHONPATH"] = str(source)
    os.environ["PYTHONSAFEPATH"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ.pop("PYTHONPYCACHEPREFIX", None)
    sys.pycache_prefix = None
    sys.dont_write_bytecode = True

    # The installed launcher has already read and verified the target. Its
    # imports must never leak into the Studio now booting from the release.
    for name in list(sys.modules):
        if name == "agent_office" or name.startswith("agent_office."):
            del sys.modules[name]
    sys.meta_path.insert(0, _SelectedOffice(source))
    return import_module("agent_office.studio_server")


def _serve(target, db, args):
    config, selected = deployment.read_selection(target)
    if selected is None:
        raise ValueError("No runtime version has been selected")
    if selected.keys() != {"seq", "sha", "activation_id"}:
        raise ValueError("Invalid runtime selection")
    receipt = {key: config[key] for key in ("target_id", "release_id", "journal_id", "port")}
    receipt.update(selected, boot_id=uuid4().hex, pid=os.getpid())
    runtime_ops._receipt(config, receipt)
    slot = deployment.verify_slot(target, selected["sha"])
    if db.is_relative_to(target) or release_view._journal_id(db) != config["journal_id"]:
        raise ValueError("Expected the configured existing runtime journal outside the deployment target")
    runtime_permissions.require_readonly(target, slot)
    db_identity = db.stat()
    module = _load_slot(slot)
    options = {}
    if args.remote:
        remote_host = import_module("agent_office.studio_ssh").RemoteHost
        options["remotes"] = [remote_host(alias, int(port), journal) for alias, port, journal in args.remote]
    if args.accept_remote:
        options["accept_remote"] = True
    for key in ("improvement_source", "release_view"):
        if path := getattr(args, key):
            options[key] = path.expanduser().resolve()
    with module.StudioServer(db, config["port"], **options) as server:
        if (server.server_address != ("127.0.0.1", config["port"])
                or server.journal_id != config["journal_id"]
                or not os.path.samestat(db_identity, server.identity)):
            raise ValueError("Studio bound a different endpoint or runtime journal")
        server.deployment_target = target

        class ManagedHandler(server.RequestHandlerClass):
            def do_GET(self):
                if self.command == "GET" and self.path == "/api/runtime-health":
                    if not self._allowed(True):
                        self._error(403, "FORBIDDEN")
                    else:
                        try:
                            available = (os.path.samestat(db_identity, db.stat())
                                         and release_view._journal_id(db) == receipt["journal_id"])
                        except (ValueError, RuntimeError, OSError, sqlite3.Error):
                            available = False
                        if available:
                            self._reply(200, receipt)
                        else:
                            self._error(503, "STORE_IO")
                    return
                super().do_GET()

            def end_headers(self):
                if self.path == "/api/hosts":
                    self.send_header("X-Agent-Office-Boot", receipt["boot_id"])
                super().end_headers()

        server.RequestHandlerClass = ManagedHandler
        print(f"Agent Office managed Studio: http://127.0.0.1:{config['port']}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the selected Studio under its provisioned service account")
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--accept-remote", action="store_true")
    parser.add_argument("--remote", nargs=3, action="append", default=[], metavar=("SSH_ALIAS", "PORT", "JOURNAL_ID"))
    parser.add_argument("--improvement-source", type=Path)
    parser.add_argument("--release-view", type=Path)
    args = parser.parse_args(argv)
    try:
        _serve(args.target.expanduser().resolve(strict=True), args.db.expanduser().resolve(strict=True), args)
    except (ImportError, ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f"Managed Studio startup failed: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

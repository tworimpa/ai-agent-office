import os
from pathlib import Path
import socket
import sys
from uuid import uuid4

import pytest

from agent_office import service_binding as binding
from agent_office.store import TaskStore


@pytest.fixture
def unit(tmp_path):
    journal = tmp_path / "runtime.db"
    jid = uuid4().hex
    with TaskStore(journal) as store:
        store.connection.execute("CREATE TABLE studio_identity(singleton INTEGER PRIMARY KEY, id TEXT)")
        store.connection.execute("INSERT INTO studio_identity VALUES(1,?)", (jid,))
    target = tmp_path / "target"
    target.mkdir()
    python = os.path.abspath(sys.executable)
    config = dict(manager="systemd", service="office-test.service", port=8766, target_id="a"*32,
                  release_id="b"*32, journal_id=jid, runtime_python=python)
    args = [python, "-I", "-m", "agent_office.managed_runtime", "--db", str(journal), "--target", str(target)]
    return target, config, args


def test_service_must_launch_the_registered_target_and_journal(unit):
    target, config, args = unit
    binding._validate_launcher(args, config, target)
    for invalid in (args[1:], [*args, "--target", str(target)], [*args, "--other"],
                    [*args[:-1], str(target.parent)], [args[0], "-c", "print(1)"], [*args[:-1], "relative"]):
        with pytest.raises(ValueError):
            binding._validate_launcher(invalid, config, target)
    with pytest.raises(ValueError, match="journal"):
        binding._validate_launcher(args, config | {"journal_id": "f"*32}, target)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native Linux service metadata")
def test_linux_binding_follows_service_account_and_command_across_restarts(unit, monkeypatch):
    target, config, args = unit
    uid = os.geteuid() + 100
    props = {"ExecStartEx": [[args[0], args, [], 0]], "User": str(uid), "Group": "", "Type": "exec", "MainPID": 0}

    def bus(manager, action, *params):
        assert manager == "systemd"
        if action == "call":
            assert params[-2:] == ("s", config["service"])
            return ["/unit"]
        return props[params[-1]]

    monkeypatch.setattr(binding, "_bus", bus)
    original = binding.inspect_service(config, target)
    props["MainPID"] = 123
    restarted = binding.inspect_service(config, target)
    assert restarted == original | {"pid": 123}
    props["User"] = str(uid + 1)
    assert binding.inspect_service(config, target)["signature"] != original["signature"]
    props["User"] = "0"
    with pytest.raises(ValueError):
        binding.inspect_service(config, target)
    if os.geteuid():
        props["User"] = str(os.geteuid())
        with pytest.raises(ValueError, match="separate accounts"):
            binding.inspect_service(config, target)
        assert binding.inspect_service(config, target, for_control=False)["pid"] == 123
    props["User"] = str(uid)
    props["ExecStartEx"] = [[args[0], [args[0], "-m", "unrelated"], [], 0]]
    with pytest.raises(ValueError):
        binding.inspect_service(config, target)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux procfs ownership proof")
def test_linux_proof_requires_a_listening_socket_in_the_unit_process():
    native = {"manager": "systemd", "pid": os.getpid()}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        assert not binding.owns_listener(native, None, port)
        listener.listen()
        assert binding.owns_listener(native, None, port)
        assert binding.owns_listener(native, {"pid": os.getpid()}, port)
        assert not binding.owns_listener(native, {"pid": os.getppid()}, port)
        assert not binding.owns_listener(native | {"pid": os.getppid()}, None, port)

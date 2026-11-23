import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from agent_office import windows_binding as binding


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows process owner verification")


def _windows_error(code):
    import pywintypes
    return pywintypes.error(code, "OpenProcess", "process metadata unavailable")


def test_access_denied_uses_real_cim_for_current_loopback_listener():
    import win32api
    from agent_office.windows_auto_task import _environment

    system = Path(win32api.GetSystemDirectory())
    clean = _environment(system, Path(os.environ["USERPROFILE"]), Path(os.environ["APPDATA"]),
                         Path(os.environ["LOCALAPPDATA"]), tools=())
    assert Path(clean["PATH"]) == system
    # Exercise the clean operator environment in its own process. On Windows
    # clearing and restoring os.environ can remove inherited empty native values.
    script = """
import os, socket, pywintypes
from unittest.mock import patch
from agent_office import windows_binding as binding
with patch.object(binding, "_birth_time", side_effect=pywintypes.error(5, "OpenProcess", "denied")):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        assert binding.owns_windows_process(os.getpid(), os.getpid(), sock.getsockname()[1])
"""
    subprocess.run([sys.executable, "-I", "-c", script], env=clean, check=True, timeout=20)


@pytest.fixture
def service_chain(monkeypatch):
    monkeypatch.setattr(binding, "_listener", Mock(return_value=101))
    monkeypatch.setattr(binding, "_parents", lambda: {101: 102, 102: 103, 103: 1})
    monkeypatch.setattr(binding, "_sc_config", lambda _: (None, None, 103))
    monkeypatch.setattr(binding, "_birth_time", Mock(side_effect=_windows_error(5)))
    query = Mock(return_value=subprocess.CompletedProcess([], 0, b"103 1 20\n102 103 30\n101 102 40\n"))
    monkeypatch.setattr(binding.subprocess, "run", query)
    return query


def _verify():
    return binding.owns_windows_process(101, 103, 8766, service="Office")


def test_cim_queries_only_the_full_observed_chain(service_chain):
    assert _verify()
    args, kwargs = service_chain.call_args
    assert "SELECT ProcessId,ParentProcessId,CreationDate FROM Win32_Process WHERE ProcessId=101 OR ProcessId=102 OR ProcessId=103" in args[0][-1]
    assert kwargs["timeout"] <= 6
    assert kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize("rows", [
    b"101 102 40\n103 1 20\n",                      # Missing intermediate process.
    b"101 102 40\n102 103 45\n103 1 20\n",          # Recycled intermediate PID.
    b"101 102 40\n102 103 30\n103 1 35\n",          # Recycled SCM PID.
    b"101 102 40\n102 103 30\n103 1 30\n",          # Birth order indistinguishable.
    b"101 102 40\n102 150 30\n103 1 20\n",          # Parent changed since snapshot.
    b"101 102 40\n101 102 40\n103 1 20\n",          # Duplicate PID.
    b"101 102 9999999999999999999\n102 103 30\n103 1 20\n",
    b"101 102 40\n102 103 missing\n103 1 20\n",
    b"x" * 193,
])
def test_unverified_cim_ancestry_fails_closed(service_chain, rows):
    service_chain.return_value.stdout = rows
    assert not _verify()


def test_cim_timeout_fails_closed(service_chain):
    service_chain.side_effect = subprocess.TimeoutExpired([], 6)
    assert not _verify()


def test_missing_process_does_not_retry_with_cim(service_chain, monkeypatch):
    monkeypatch.setattr(binding, "_birth_time", Mock(side_effect=_windows_error(87)))
    assert not _verify()
    service_chain.assert_not_called()


def test_changed_listener_or_scm_owner_rejects_before_cim(service_chain, monkeypatch):
    monkeypatch.setattr(binding, "_listener", Mock(side_effect=[101, 104]))
    assert not _verify()
    monkeypatch.setattr(binding, "_sc_config", lambda _: (None, None, 104))
    assert not _verify()
    service_chain.assert_not_called()


@pytest.mark.parametrize("replacement", ["listener", "service"])
def test_change_during_cim_query_fails_closed(service_chain, monkeypatch, replacement):
    def change_owner(*args, **kwargs):
        if replacement == "listener":
            monkeypatch.setattr(binding, "_listener", lambda port: 104)
        else:
            monkeypatch.setattr(binding, "_sc_config", lambda name: (None, None, 0))
        return service_chain.return_value

    service_chain.side_effect = change_owner
    assert not _verify()

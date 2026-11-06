"""OS-native end-to-end smoke of CLIAdapter using local fake worker subprocesses.

No cloud accounts, secrets, model calls or paid usage.
"""
import json
import sys

import pytest

from agent_office.adapters import CLIAdapter


class StubCLI(CLIAdapter):
    def command(self, prompt: str, *, allow_edits: bool) -> list[str]:
        if self.engine == "claude":
            source = (
                "import json,sys;"
                "print(json.dumps({'result':sys.stdin.read(),'session_id':'local-claude'}))"
            )
        else:
            source = (
                "import json,sys;"
                "msg=sys.stdin.read();"
                "print(json.dumps({'type':'thread.started','thread_id':'local-codex'}));"
                "print(json.dumps({'type':'item.completed','item':"
                "{'type':'agent_message','text':msg}}))"
            )
        return [sys.executable, "-c", source]


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_real_subprocess_stdin_roundtrip(engine, tmp_path):
    prompt = "Fix login: PowerShell & | < > % \"quotes\"\n한국어 경로"
    result = StubCLI(engine).execute(prompt, workspace=tmp_path, allow_edits=False)
    assert result.returncode == 0
    assert result.message == prompt
    assert result.session_id == f"local-{engine}"


def test_not_found_error(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_office.adapters.shutil.which", lambda name: None)
    result = CLIAdapter("claude").execute("test", workspace=tmp_path, allow_edits=False)
    assert result.returncode == 127
    assert "Missing CLI" in result.message

"""A revoked provisional reply cannot leave any transport or strand the waiting session."""
from contextlib import contextmanager
from pathlib import Path
import sys
import uuid

import pytest

from agent_office.adapters import CLIAdapter
from agent_office.dialogue import DelegationWithdrawn
from test_acp_adapters import Answers, run_fixture
import test_batch_dialogue as batch
import test_codex_inbox as codex


class WithdrawFirstReply:
    def __init__(self, human):
        self.human = human
        self.original = None
        self.withdrawn = False
        self.reused = False

    async def ask(self, kind, prompt, session):
        row = await self.human.ask(kind, prompt, session)
        if kind == "permission":
            return row
        assert self.original is None
        self.original = row
        return dict(row, answer="withdrawn-value", source="delegate")

    @contextmanager
    def sending(self, request):
        if request == self.original["id"] and not self.withdrawn:
            self.withdrawn = True
            raise DelegationWithdrawn()
        with self.human.sending(request):
            yield

    async def await_operator(self, request):
        assert self.withdrawn and not self.reused and request == self.original["id"]
        self.reused = True
        return self.original

    def delivered(self, request):
        self.human.delivered(request)


@pytest.mark.parametrize("engine", ["codex", "claude", "antigravity", "gemini", "cline"])
def test_original_session_uses_operator_reply_after_withdrawal(tmp_path, monkeypatch, engine):
    if engine == "codex":
        channel = WithdrawFirstReply(codex.Operator())
        _, result = codex.fixture(tmp_path, monkeypatch, "approve", operator=channel)
    elif engine in ("gemini", "cline"):
        channel = WithdrawFirstReply(Answers())
        result = run_fixture(tmp_path, monkeypatch, engine, scenario="inbox_question", edit=engine == "cline", inbox=channel)
    else:
        adapter = CLIAdapter(engine, timeout=15)
        args = adapter.command("", allow_edits=False)[1:]
        cmd = [sys.executable, str(Path(batch.__file__)), engine, str(uuid.uuid4()), "success", *args]
        monkeypatch.setattr(adapter, "command", lambda *a, **kw: cmd)
        channel = WithdrawFirstReply(batch.Operator())
        result = adapter.execute("Original specification", workspace=tmp_path, allow_edits=False, inbox=channel)
    assert result.returncode == 0, result.message
    assert channel.reused and channel.withdrawn

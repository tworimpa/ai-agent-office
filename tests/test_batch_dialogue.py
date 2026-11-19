import json
from contextlib import contextmanager
from pathlib import Path
import sys
import uuid

import pytest

from agent_office.adapters import CLIAdapter


class Operator:
    def __init__(self):
        self.questions = []
        self.sent = []
        self.confirmed = []

    async def ask(self, kind, prompt, session):
        assert kind == "question"
        self.questions.append((prompt, session))
        return {"id": "question-1", "decision": "answer", "answer": 'spec-v2 & "기술 답변"'}

    @contextmanager
    def sending(self, request_id):
        self.sent.append(request_id)
        yield

    def delivered(self, request_id):
        self.confirmed.append(request_id)


@pytest.mark.parametrize("engine", ["claude", "antigravity"])
@pytest.mark.parametrize("case", ["success", "wrong-session", "bad-session", "provider-error"])
def test_resume_only_original_batch_session(tmp_path, monkeypatch, engine, case):
    sid = str(uuid.uuid4())
    adapter = CLIAdapter(engine, timeout=15)
    original = adapter.command("", allow_edits=False)[1:]
    monkeypatch.setattr(adapter, "command", lambda *args, **kwargs: [
        sys.executable, __file__, engine, sid, case, *original])
    user = Operator()
    result = adapter.execute("Original specification", workspace=tmp_path, allow_edits=False, inbox=user)
    if case == "success":
        assert result.returncode == 0 and result.message == "continued" and result.session_id == sid
        assert user.confirmed == ["question-1"]
    else:
        assert result.returncode != 0 and user.confirmed == []
    if case in ("bad-session", "provider-error"):
        assert user.questions == user.sent == []
    else:
        assert user.questions == [("Which spec?", sid)] and user.sent == ["question-1"]


def fixture():
    engine, session, case, *args = sys.argv[1:]
    raw = sys.stdin.read()
    if engine == "antigravity":
        raw = json.loads(raw)["message"]["content"]
    resume = "--resume" if engine == "claude" else "--conversation"
    if resume in args:
        assert args[args.index(resume) + 1] == session
        assert 'spec-v2 & "기술 답변"' in raw and "permission" in raw
        text = "continued"
        if case == "wrong-session":
            session = str(uuid.uuid4())
    else:
        assert "Original specification" in raw and "AGENT_OFFICE_QUESTION" in raw
        text = "AGENT_OFFICE_QUESTION\nWhich spec?"
        if case == "bad-session":
            session = "--latest"
    if engine == "claude":
        assert args[args.index("--permission-mode") + 1] == "plan"
        result = dict(type="result", subtype="success", is_error=(case == "provider-error"), result=text,
                      session_id=session, stop_reason="end_turn")
    else:
        assert "--sandbox" in args and args[args.index("--mode") + 1] == "plan"
        result = dict(status="ERROR" if case == "provider-error" else "SUCCESS", response=text,
                      conversation_id=session)
    print(json.dumps(result))


def test_expired_decision_does_not_launch_after_sending_guard(tmp_path, monkeypatch):
    from agent_office import batch_dialogue
    clock = [1.0]
    monkeypatch.setattr(batch_dialogue.time, "monotonic", lambda: clock[0])
    @contextmanager
    def blocked_writer():
        clock[0] = 3.0
        yield
    monkeypatch.setattr(batch_dialogue.subprocess, "Popen", lambda *a, **kw: pytest.fail("Late provider launch"))
    with pytest.raises(TimeoutError):
        batch_dialogue._turn("claude", ["unused"], "reply", tmp_path, {}, 2.0, blocked_writer())


if __name__ == "__main__":
    fixture()

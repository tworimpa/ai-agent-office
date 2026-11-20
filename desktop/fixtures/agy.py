"""Antigravity reviewer fixture for the installed desktop's default role profile."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from meeting import meeting_result


flags = ["--input-format", "stream-json", "--output-format", "stream-json", "--mode", "plan",
         "--sandbox", "--disable-slash-commands", "--print-timeout"]
args = sys.argv[1:]
if args == ["--version"]:
    print("agy 1.0.0-fixture")
elif args == ["--help"]:
    print(" ".join(flags))
else:
    assert args[:-1] == flags and args[-1].endswith("s")
    envelope = json.load(sys.stdin)
    assert envelope["event"] == "user"
    prompt = envelope["message"]["content"]
    if prompt.startswith("MEETING_REVIEWER"):
        output = json.dumps(meeting_result(prompt), ensure_ascii=False)
    else:
        assert "Review the requested changes" in prompt and "1 passed" in prompt
        assert Path("note.txt").read_text(encoding="utf8") == "desktop verified\n"
        output = "VERDICT: APPROVED\n지정 문장과 테스트를 확인했습니다."
    print(json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "response": output, "conversation_id": "desktop-agy"}}))

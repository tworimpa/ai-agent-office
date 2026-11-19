"""Read-only specialist and verifier stand-ins, each run in its own evidence directory."""
import json
import os
from pathlib import Path
import sys


def main():
    engine = sys.argv[1]
    if "--version" in sys.argv or "--help" in sys.argv:
        print("1.2.3 --print --input-format --output-format --mode --sandbox")
        return
    raw = sys.stdin.read()
    prompt = json.loads(raw)["message"]["content"] if engine == "agy" else raw
    role = prompt.splitlines()[0]
    assert role in {"AWAY_SPECIALIST", "AWAY_VERIFIER"}
    mode_flag = "--sandbox" if engine == "agy" else "--permission-mode"
    assert mode_flag in sys.argv and "plan" in sys.argv
    assert not Path("web").exists()
    evidence = json.loads(Path("evidence.json").read_text(encoding="utf-8"))
    assert evidence["topic"] == "python_version"
    with Path(os.environ["OFFICE_AWAY_CALLS"]).open("a", encoding="utf-8") as file:
        file.write(json.dumps([role, str(Path.cwd())]) + "\n")
    portfile = os.environ.get("OFFICE_DELAY_" + role.removeprefix("AWAY_"))
    if portfile:
        import socket
        import time
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            Path(portfile).write_text(str(sock.getsockname()[1]))
            time.sleep(60)
    citation = evidence["evidence"][0]
    value = {"answer": ">=3.11", **citation}
    if role == "AWAY_VERIFIER":
        value["verdict"] = "verified"
    message = json.dumps(value)
    if engine == "agy":
        print(json.dumps({"status": "SUCCESS", "response": message}))
    else:
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": message}))


if __name__ == "__main__":
    main()

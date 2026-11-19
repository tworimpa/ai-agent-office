"""Call one public candidate function. Only the parent evaluates the response."""

import json
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path.cwd() / "src"))
    from agent_office.events import sanitize_payload, snapshot_request
    from agent_office.models import validate_id

    methods = {"payload": sanitize_payload, "request": snapshot_request, "id": validate_id}
    operation, value = json.load(sys.stdin)
    try:
        response = ["returned", methods[operation](value)]
    except ValueError:
        response = ["rejected"]
    print(json.dumps(response))


if __name__ == "__main__":
    main()

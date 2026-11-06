#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m compileall -q src tests
python -m pytest -q

echo "== Available CLI versions (not required for unit tests) =="
python -m agent_office.cli doctor || true

if [[ "${1:-}" == "--live" ]]; then
  python -m agent_office.cli doctor --require claude codex
  python -m agent_office.cli run --repo . --developer claude \
    --task "Read this repository and propose one safe improvement. Do not modify files."
  python -m agent_office.cli run --repo . --developer codex \
    --task "Review the architecture in read-only mode. Do not modify files."
fi

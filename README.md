# AI Agent Office

**AO-000 Bootstrap** — local-first orchestration of existing coding agent CLIs through LangGraph.

This is an early, deliberately limited command-line MVP. It does **not** implement the desktop office UI, a persistent job scheduler, automatic human-in-the-loop Q&A, unattended delegation, GitHub publishing, or self-updating. Those are planned features.

## Architecture

```text
User task -> Agent Office CLI
            -> Git worktree (isolated working directory)
            -> LangGraph
               -> Developer CLI (Claude Code or Codex)
               -> Local test command (allow-edits mode only)
               -> Reviewer CLI (read-only)
               -> [rework on failure, with a maximum attempt limit]
            -> Report for human review
```

The orchestrator does not replace the internal agentic loops inside Claude Code or Codex. It treats each CLI as a worker.

## Requirements

- Python 3.11+ and Git installed locally.
- Claude Code and/or Codex CLI installed and authenticated for real runs.
- Windows or Linux, with a local Git repository containing an initial commit.
- The CLI you select must be on `PATH`.

## Install

```bash
git clone https://github.com/tworimpa/ai-agent-office.git
cd ai-agent-office
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## Try it

Default is **planning-only**, with edit permissions disabled.

```bash
agent-office run --repo . --task "Suggest the next milestone for Agent Office"
```

Opt in to edit the isolated Git worktree and run tests:

```bash
agent-office run --repo . --allow-edits \
  --task "Add one focused regression test and explain what changed"
```

By default development uses `claude` and review uses `codex`. Override with `--developer codex --reviewer claude`. A task is **not merged, pushed or deployed** even after the reviewer approves it. Inspect the generated worktree and branch first.

**Important:** CLI sandbox modes are provider-specific; Claude's `acceptEdits` permission mode is **not a full OS/filesystem sandbox**. Prompts are not a security boundary. Only run trusted tasks on an appropriately isolated local account/environment, and do not expose sensitive credentials or production repositories. The default test command is `python -m pytest -q` (using the Agent Office Python interpreter). Changes to files outside the Git worktree can still be possible depending on CLI permissions and installed tools.

## Roadmap

- AO-000: LangGraph + Claude/Codex adapters + separate worktree + tests + human-reviewed result.
- AO-001: persistent task database, event stream, and an Agent Office UI.
- AO-002: Gemini/Cline/Antigravity adapters and skills registry.
- AO-003: questions, approval inbox, session continuation, and delegated decisions within policy.
- AO-004: supervised self-improvement, regression benchmarks, candidate builds and safe promotion.

## Project rules

- Never merge, publish, deploy or modify approval/security policy automatically.
- Isolate development to a worktree; never edit the running Agent Office installation in place.
- Store per-task evidence including test results, git diffs and decision trail before promotion.
- Keep agent roles separate from the selected CLI engines.
- Fail closed on unrecognized reviewer verdicts or provider errors.

See [docs/architecture.md](docs/architecture.md) for the planned system and AO-000 scope.

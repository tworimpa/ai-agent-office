# AI Agent Office — ChatGPT session handoff to local Codex

**Date:** 2026-11-06 KST  
**Repo:** https://github.com/tworimpa/ai-agent-office  
**PR #1:** https://github.com/tworimpa/ai-agent-office/pull/1  
**AO-001 Issue #2:** https://github.com/tworimpa/ai-agent-office/issues/2

> This document is a comprehensive project/decision handoff drawn from the ChatGPT conversation. It is **not a verbatim chat export**.

## User's product vision

Build a personal software development team running on **Windows and Linux**, with a visual office where multiple CLI coding agents cooperate and users see each agent's state, assigned tasks, skills, tool calls, conversations, decisions, and work products. Support:
- Existing coding CLIs: Claude Code, OpenAI Codex, Gemini CLI, Cline CLI, Antigravity CLI.
- Natural-language task intake, project/task board, later API/CLI requests.
- Roles such as Planner, Supervisor, Architect, Backend/Frontend Developer, Tester, Reviewer, Domain Expert. Roles are independent from which underlying CLI executes them.
- Separate agent-specific skills, shared tool connectors (MCP), collaboration messages and UI event stream.
- Workers use their **own** tool-use/agentic loops; LangGraph provides **outer** planning, routing, retries, cross-agent coordination, persistence and interruptions.
- A CLI Adapter per engine normalizes commands, JSON/JSONL, session IDs, failure, restart, cancellation and eventually live events.
- Git worktrees per task and distinct stable vs candidate execution environments.
- Long-term goal: **Agent Office can develop Agent Office itself** from user instructions, test candidate releases and propose improvements, while keeping the running stable version protected.

## Important decisions already made

1. GitHub: `tworimpa/ai-agent-office` is the canonical project.
2. Windows and Linux are **both** first-class validation targets.
3. Python + LangGraph is the initial orchestrator. Start with Claude Code + Codex adapters, then add Gemini/Cline/Antigravity.
4. Run tests, review code, and explicitly bound rework retries; do not treat LLM review alone as a release pass.
5. User controls merging to `main`, deployments, irreversible operations, expanded tool permissions, and changes to security/approval policy.
6. For unattended operation, agents may autonomously resolve **preapproved, low-risk** technical questions via Delegate + Independent Verifier, but they must not fabricate authorization.
7. Decisions made while user is absent must have an audit trail (question, answer, policy, evidence, changes, tests) and a **return report**. Questions and approvals are separate event types.
8. At each step, give the user an accurate status; do not claim work has been executed when it was only proposed.

## AO-000 status — checked 2026-11-06

Branch: `feature/ao-000-bootstrap`, Draft PR #1, not merged. Last *code* commit before this handoff: `190ff436849bb0e8fd9ba83eabd4568f0113aa63`.

Implemented:
- `src/agent_office/adapters.py`: Claude Code / Codex CLI adapters; read-only vs edit flag; JSON/JSONL output parser; prompts via stdin, Windows UTF-8 subprocess handling.
- `src/agent_office/workflow.py`: LangGraph Develop → Test → Review → limited retry, reviewer fail-closed.
- `src/agent_office/workspace.py`: separate Git worktree and branch per task.
- `src/agent_office/cli.py`: `agent-office run` and `agent-office doctor`.
- `src/agent_office/diagnostics.py`: installed CLI/Git version diagnostics.
- `scripts/verify-windows.ps1`, `scripts/verify-linux.sh`, `docs/verification.md`.
- `.github/workflows/ci.yml` runs Windows + Ubuntu × Python 3.11 + 3.13.
- Tests cover graph flow, CLI adapters, subprocess stdin+Korean text, git worktrees and diagnostics.

**Verified:** GitHub Actions run https://github.com/tworimpa/ai-agent-office/actions/runs/42151022945 was SUCCESS: all four OS/Python combinations had **18 passed** each. An earlier Windows Korean stdin failure was corrected.

**Unverified:** Real user-machine authenticated Claude Code and Codex executions on Windows/Linux, actual vendor CLI flags/session behavior, and isolated *edit-mode* acceptance on a disposable clone. Mock CI does not validate those. No auto merge/deployment.

## Command line setup for user PC

### Windows PowerShell

```powershell
git clone https://github.com/tworimpa/ai-agent-office.git
cd ai-agent-office
git switch feature/ao-000-bootstrap
powershell -ExecutionPolicy Bypass -File .\scripts\verify-windows.ps1
codex --sandbox workspace-write --ask-for-approval on-request
```

### Linux

```bash
git clone https://github.com/tworimpa/ai-agent-office.git
cd ai-agent-office
git switch feature/ao-000-bootstrap
bash scripts/verify-linux.sh
codex --sandbox workspace-write --ask-for-approval on-request
```

If already cloned, skip `git clone`. First inspect `git status -sb`, then `git fetch origin` and switch/update the branch without discarding local changes. Codex automatically reads root-level `AGENTS.md` when a session starts.

## First prompt to paste into Codex

Read AGENTS.md and docs/CODEX_HANDOFF_2026-11-06.md, then README.md, docs/architecture.md, docs/decisions.md, docs/verification.md. Check the actual repository/branch status and review PR #1 and Issue #2 if GitHub access is available. Run AO-000 tests on this OS and report exact results. Check whether Claude Code and Codex CLI are installed and authenticated, but do not invoke paid/cloud live checks unless I allow them. Do not merge, push, deploy or change permissions without explicit authorization. Make a safe plan to implement AO-001 SQLite task persistence and event recording in an isolated branch/worktree, preserving both Windows and Linux. After implementing, show test output, changes and remaining blockers. Respond in Korean.

## AO-001 next development — issue #2

1. SQLite tasks: task_id, project, request, assigned role/engine, workspace, status, retry_count and timestamps.
2. Append-only events: run_id, seq, task/agent IDs, event_type, timestamp, redacted/bounded payload.
3. State transitions: QUEUED/RUNNING/TESTING/REVIEWING/WAITING_USER/READY_FOR_HUMAN_REVIEW/FAILED/CANCELED; crash recovery must distinguish **RECOVERY_REQUIRED** from safe restart.
4. Read-only tasks/events CLI or API, eventually support UI event streaming over WebSocket or AG-UI.
5. Windows/Linux tests for DB restart, event order, idempotency, redaction and interruption.
6. Keep approvals, auto merge/deploy, and dangerous tool execution outside MVP.

## Future modules

- AO-002: adapters for Gemini/Cline/Antigravity, Skills Registry and visual event stream.
- AO-003: Question Router, HITL, user Inbox, approval queue, CLI session resume, specialist agent delegation, Decision Policy, Decision Ledger and post-absence report.
- AO-004: self-improvement analyzer, proposal→candidate→regression benchmark→human-approved release promotion, rollback.

## Key design pitfalls

- Separate LangGraph interrupt nodes from external CLI process nodes; interrupted nodes may re-execute, causing duplicated side effects.
- A worktree only isolates Git workspace; it is not OS process, network, secret or filesystem sandboxing.
- Do not confuse ordinary questions with OS/tool permission requests; another LLM cannot approve human-only actions.
- On provider failures, timeouts or unauthenticated CLI, fail closed and retain evidence.
- Distinguish real successful execution from planned design, simulation, mock tests and unverified assumptions.

## Useful verification commands

```bash
git status -sb
git log --oneline -5
python -m pytest -q
python -m agent_office.cli doctor
```

For authenticated provider checks, see `docs/verification.md`. Only execute against non-sensitive test repos with user-granted permissions.

## Conversation topic timeline

1. Open-source investigation of a personal Windows/Linux multi-agent office.
2. Agentic Loop architecture and comparison (LangGraph, OpenHands, CrewAI); Agentic Loop versus Agent Orchestration.
3. Integration of Claude Code, Codex, Gemini, Cline, Antigravity as CLI workers.
4. Handling in-progress agent questions, user approval, stateful pause/resume.
5. Auto-answering low-risk technical questions during absence using another expert agent; auditing decisions for later reports.
6. Self-development: Agent Office operates on its own repository, with stable/candidate separation and human-controlled release.
7. AO-000 implementation in GitHub PR #1.
8. Windows/Linux equal support; real CI 4/4 green, 18 tests per matrix job.
9. Handoff to local Codex for continued implementation, with AO-001 task/event persistence next.

To transfer **literal** original ChatGPT messages instead of this structured summary, independently copy the chat or use ChatGPT data export; do not assume Codex can automatically read a ChatGPT conversation.

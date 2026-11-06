# Codex project guidance — AI Agent Office

Read `docs/CODEX_HANDOFF_2026-11-06.md` before working on the project. Also read `README.md`, `docs/architecture.md`, `docs/decisions.md`, and `docs/verification.md`.

This project runs on BOTH Windows and Linux. Use LangGraph as the outer coordinator of existing coding CLI agents (Claude Code, Codex, Gemini, Cline, Antigravity); each CLI retains its own agentic loop. Separate task roles from CLI engines. Communicate with the user in Korean.

Do not auto-merge, push, deploy, change security policies, access private credentials, bypass sandbox rules or act on risk-sensitive approval decisions without the user's explicit permission. Keep stable runtime isolated from development candidates; use a separate worktree/branch for implementation. Git worktrees are not process/OS sandboxes.

Run tests and report exact evidence. In particular distinguish mock subprocess CI from actual authenticated vendor CLI testing. On failure, do not claim completion. Preserve original task context, question/approval distinctions, and human review gates.

First verify branch/CI/current code before continuing AO-001. The handoff is a historical snapshot, not an assertion that the remote state has not changed.

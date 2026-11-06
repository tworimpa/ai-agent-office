# Architecture Decision Records

## ADR-001 — Orchestrate existing coding CLIs

Decision: Use LangGraph for outer orchestration. Keep each CLI's own reasoning/tool loop intact.

## ADR-002 — Roles are separate from engines

A Developer/Reviewer role may be fulfilled by Claude Code, Codex, or a future registered CLI engine. Engines must never be treated as inherently privileged.

## ADR-003 — No automatic deployment

Agent Office may improve its own source in a worktree, but merging to the stable branch, updating its own process, changing credentials, and modifying security/approval policy need explicit user approval.

## ADR-004 — Human-in-the-loop and unattended delegation

Planned: questions are structured events, and a persisted queue may route low-risk technical questions to a delegated agent while away. A separate deterministic policy check must allow any delegated decision. Authorization and security approvals cannot be synthesized from another model's answer.

## ADR-005 — Validate before reporting completion

A reviewer model's approval is not sufficient. Require executable tests and independently reviewable artifacts; keep outcomes distinguishable from actual deployment.

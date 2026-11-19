# Architecture Decision Records

## ADR-001 — Orchestrate existing coding CLIs

Decision: Use LangGraph for outer orchestration. Keep each CLI's own reasoning/tool loop intact.

## ADR-002 — Roles are separate from engines

A Developer/Reviewer role may be fulfilled by Claude Code, Codex, or a future registered CLI engine. Engines must never be treated as inherently privileged.

## ADR-003 — No automatic deployment

Agent Office may improve its own source in a worktree, but merging to the stable branch, updating its own process, changing credentials, and modifying security/approval policy need explicit user approval.

The owner's 2026-11-06 standing authorization in AGENTS.md supplies that explicit approval for Codex maintenance branch push, PR and CI-gated source merge. It does not authorize deployment, automatic worker self-promotion, private credential inspection or protection bypass.

## ADR-006 — Complete authorized maintenance through PR and CI

For owner-requested work, Codex proceeds through normal feature push, a reviewable PR, latest-head Windows/Linux CI, remediation and guarded merge without repeating the same permission question. Failed checks, unresolved blocking reviews and repository protections are not waived. Clean local main checkouts may be fast-forwarded and verified; unrelated/dirty work is preserved. See AGENTS.md for the scope and exact owner instruction.

## ADR-004 — Human-in-the-loop and unattended delegation

Studio persists typed questions and permission requests. For an explicitly delegated question the execution host checks the immutable run policy, reads the named evidence at the pinned Git revision, and requires independent specialist and verifier results. The final handoff is serialized with human answers, revocation and run stop. Permission requests stay with the operator. See the [away delegation plan](superpowers/plans/2026-11-19-away-delegation.md).

## ADR-005 — Validate before reporting completion

A reviewer model's approval is not sufficient. Require executable tests and independently reviewable artifacts; keep outcomes distinguishable from actual deployment.

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

## ADR-007 — The host may publish a reviewed result with the operator's `gh` login

Owner decision, 2026-11-27. Product workers still never commit, push, merge or deploy, and their environment keeps GitHub CLI and Git transport disabled. When the operator explicitly asks for it on a finished run, the Studio host process may commit that run's worktree changes with the operator's Git identity, push the branch and open a draft pull request using the `gh` login already present on that computer. The host records the commit, branch and pull request in the run journal. Merging stays with a person on the pull request. The GitHub App connection remains the route for unattended issue-driven publication. Design: [product analysis, chapter 12](product-analysis-2026-11-27.md).

## ADR-008 — Prefer Codex; keep Antigravity as an implemented option

Owner decision, 2026-11-27. The Antigravity (`agy`) adapter and its selection stay supported, but defaults and newly built features use Codex where possible. Google's published guidance says wrapping `agy` from third-party applications is not supported on consumer plans, so it should not sit on a default path. Changing the default reviewer is a separate implementation change because the current default pair also defines how records and requests without stored engines are interpreted. Until that change merges, the code default and the profile wording in AGENTS.md remain Codex + Antigravity. Details: [product analysis, revision 2](product-analysis-2026-11-27.md).

## ADR-009 — Archive the Rust comparison build

Owner decision, 2026-11-27. The comparison concluded that Electron remains the default app on both operating systems ([results](../desktop/comparison/RESULTS.md)). The Rust comparison build receives no new development and is no longer presented as an installation choice. Its checks are still part of the release gate and the operator deployment trust policy; removing them from those lists changes that policy and needs the owner's separate, explicit approval.

Owner approval, 2026-11-28. The Rust comparison build no longer runs in CI, and its checks are no longer part of the release gate or the operator deployment trust policy. The archived source (`rust-desktop/`, `desktop/comparison/`, `scripts/ci-rust-*`) stays in the repository. The Desktop workflow still calls exactly one reusable workflow, now `.github/workflows/tested-merge.yml`, which only checks out the repository and prints the tested commit. The call is kept as a neutral anchor because GitHub records it in the run together with the merge commit the pull request run tested, and both the deployment policy (`src/agent_office/policy_github.py`) and the split-session bootstrap (`scripts/split-session-bootstrap.ps1`) read the tested merge commit from that record. Any other reusable workflow is still refused. Seven checks are required: four CI jobs, two desktop installer jobs and `tested-merge / ubuntu-latest / Tested merge`. An operator controller or split-session runtime installed before this change refuses this merge and every later one until the operator updates them ([deployment guide](studio-deployment.md)).

## ADR-010 — Adopt the Orca bot model; development and personal use first

Owner decision, 2026-11-27. Bots move toward the model reviewed in Orca: a home folder per bot, a continuing session, memory the bot maintains itself, delegation of code changes to workers, schedules and GitHub triggers. Two existing rules stay: a bot does not edit project code itself, and delegated work passes the test, independent review and human review gates (ADR-005). Distribution work such as code signing, automatic updates, localisation and additional platforms is deferred while the product serves its owner's own development. The owner authorised real Codex checks on disposable repositories for this work. Design and order of work: [product analysis, chapters 13 and 14](product-analysis-2026-11-27.md).

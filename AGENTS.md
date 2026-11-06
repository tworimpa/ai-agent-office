# Codex project guidance — AI Agent Office

Read `docs/CODEX_HANDOFF_2026-11-06.md` before working on the project. Also read `README.md`, `docs/architecture.md`, `docs/decisions.md`, and `docs/verification.md`.

This project runs on BOTH Windows and Linux. Use LangGraph as the outer coordinator of existing coding CLI agents (Claude Code, Codex, Gemini, Cline, Antigravity); each CLI retains its own agentic loop. Separate task roles from CLI engines. Communicate with the user in Korean.

The owner-selected initial engine profile is Codex + Antigravity (`agy`): default developer=codex and reviewer=antigravity, with explicit role overrides. Retain legacy Claude compatibility; do not infer authentication or real-task acceptance from installation/help or mock tests. Metadata probes use only fixed known commands, never credential files or broad environment dumps.

Keep stable runtime isolated from development candidates; use a separate worktree/branch for implementation. Git worktrees are not process/OS sandboxes. Do not deploy, change security policies, inspect private credentials, bypass sandbox/protection rules, or act on risk-sensitive approvals without explicit authorization.

## Standing owner authorization: complete PR and CI workflow

On 2026-11-06 the owner instructed: "작업 하면 PR 및 CI 확인후 머지까지 진행 할수 있도록 지침 업데이트 하고 작업 진행하라".

For implementation or fixes requested by this owner in this repository, complete the following workflow without asking again for routine branch push, PR creation or merge:

1. Work in an isolated branch/worktree, preserve unrelated local changes, implement and review the requested change, and run relevant tests.
2. Push the normal feature branch and create/update a reviewable PR targeting `main`. Record concrete behavior, validation evidence and remaining limitations in the PR.
3. Wait for every required check and the repository's full Windows/Linux CI matrix on the latest PR head. Investigate failures, fix within scope and repeat affected checks; never bypass or dismiss failing checks to merge.
4. Resolve actionable Critical/Important review findings. When checks are green, the PR is mergeable and no blocking review remains, mark it ready if necessary and merge with an expected-head-SHA guard. Preserve repository protections and do not merge unrelated PRs under this authorization.
5. Verify the remote merge. Fast-forward clean local `main` checkouts and verify the merged result. Preserve dirty checkouts instead of resetting them. Report PR/merge/CI links, commit and remaining validation limits.

This is explicit standing authorization for Codex's project-maintenance workflow. It is not authority for product workers to approve their own actions, deploy/update a running stable service, perform destructive cleanup, force-push shared history, expand permissions or bypass protections. Existing worker prompts prohibiting commit/push/merge remain in force. A missing or failed check, unresolved blocking review, protection requirement or unrelated/destructive action must be resolved or reported rather than silently overridden.

Earlier handoff/plan statements requiring a separate approval for each push/PR/merge are historical and superseded only for this maintenance workflow. Deployment, private credential access, security changes and separately authorized real-provider acceptance retain their own boundaries.

Run tests and report exact evidence. In particular distinguish mock subprocess CI from actual authenticated vendor CLI testing. On failure, do not claim completion. Preserve original task context, question/approval distinctions, and human review gates.

First verify branch/CI/current code before continuing AO-001. The handoff is a historical snapshot, not an assertion that the remote state has not changed.

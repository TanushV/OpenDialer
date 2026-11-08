# Working agreement

**The active plan is `docs/REBUILD_PLAN.md` (owner approved November 6, 2026).** Read it in full on every continuation or context compaction, including its Status section, which is the current checkpoint. Where it conflicts with anything below or with an older plan, it wins. Hosted mode, PostgreSQL and Windows were removed in its Phase 2 (R2.1): the app is one Mac and one SQLite file.

**Build rules (section 12A of that plan).** One worker, in one thread: no subagents, Workflow runs, parallel sessions, worktrees or concurrent writers. Commit directly to `main` with no feature branches, and start no new work while `main`'s CI is red. Once Phase 0 step R0.0 adds it, iterate with `uv run python scripts/check.py` (fast tier) and run `scripts/check.py full` before every push; never push a failing full check. CI changes in the same phase as the code it covers.

The plans below are history for what a feature does.

Testing handoff: `docs/HANDOFF.md`, with the code map in `docs/HANDOFF_AST.md`.
Campaign preparation: `docs/CAMPAIGN_PREP_PLAN.md` (one Markdown script, per-lead agent preparation, simple settings; implemented in 0.7.0). Script learning: `docs/SCRIPT_LEARNING.md`.
Current script update: `docs/SCRIPT_PLAYBOOK_PLAN.md` (flexible Markdown, fresh generations, token usage and the owner-provided Agite script; read on continuation).
Removal and refresh follow-up: `docs/LIST_REMOVAL_PLAN.md` (intuitive membership/list removal, recoverable views and explicit script refresh status; read on continuation).
Architecture and calling-policy review: `docs/ARCHITECTURE.md` (November 1, 2026); distinguish current behavior from the owner's requested cadence, final-call voicemail and warmup work.
Numbers and connect rate plan: `docs/NUMBER_CONNECT_PLAN.md` (November 4; read the full checklist on continuation and compaction).
Calling completion detail: `docs/CALLING_COMPLETION_PLAN.md` (November 2 owner revision; manual Space/Next call, optional DNC, follow-up scripts, streamed helper and simpler readiness).

`docs/LEAN_PLAN.md` (October 22, 2026) is superseded by the rebuild plan; its Status section remains the record through 1.0.1. `docs/REMEDIATION_PLAN.md`, `docs/EXECUTION_SPEC.md`, `docs/EXECUTION_TASKS.json` and `docs/STATUS.md` are history: consult them for what a feature does, not for mechanisms or order.

Write acceptance tests before changing code and keep them green. Add a regression test before fixing a defect. Commit coherent steps often. Never commit credentials, app databases, recordings, build products, or test caches. Never add AI attribution to commits, PRs or releases.

Tests with simulated providers are evidence about software behavior only. Carrier calls, hardware audio, production hosting, account eligibility, legal approval, and outcome lift require their own evidence. Keep those gates explicit; never convert an absent API key into a successful integration claim.

October 30 owner authorization: protect saved credentials, verify and publish the current release, and update this Mac through the signed updater. Preserve installed workspace data. Future installs/updates still require an explicit owner request.

October 30 follow-up: desktop credentials use an automatic private local key file. Do not invoke Keychain/Credential Manager or request OS password approval; preserve old encrypted copies and restore available setup. This Mac remains authorized for the signed update.

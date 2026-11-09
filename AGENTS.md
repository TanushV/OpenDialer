# Working agreement

**The active plan is `docs/REBUILD_PLAN.md` (owner approved November 6, 2026).** Read it in full on every continuation or
context compaction, including its Status section, which is the current checkpoint. Where it conflicts with anything
below, it wins. The app is one computer and one SQLite file per rep, on Mac or Windows (D2). How the app works today is in
`docs/ARCHITECTURE.md`; finished plans are history in `docs/history/` (consult them for what a feature does, not for
mechanisms or order).

**Build rules (section 12A of that plan).** One worker, in one thread: no subagents, Workflow runs, parallel sessions,
worktrees or concurrent writers. Commit directly to `main` with no feature branches, and start no new work while `main`'s
CI is red. Iterate with `uv run python scripts/check.py` (fast tier) and run `scripts/check.py full` before every push;
never push a failing full check. CI changes in the same phase as the code it covers.

**Calling rules (G10, Phase 9E).** Every blocker in `calling/blockers.py` declares its way out in `EXITS` and has an
in-and-out test in `tests/acceptance/test_blocker_exits.py`. Never add a blocker, a message that says "clears by itself",
or a screen state without its exit and test.

Write acceptance tests before changing code and keep them green. Add a regression test before fixing a defect. Commit
coherent steps often. Never commit credentials, app databases, recordings, build products, or test caches. Never add AI
attribution to commits, PRs or releases.

Tests with simulated providers are evidence about software behavior only. Carrier calls, hardware audio, production
hosting, account eligibility, legal approval, and outcome lift require their own evidence. Keep those gates explicit;
never convert an absent API key into a successful integration claim.

October 30 owner authorization: protect saved credentials, verify and publish the current release, and update this Mac
through the signed updater. Preserve installed workspace data. Future installs/updates still require an explicit owner
request.

October 30 follow-up: desktop credentials use an automatic private local key file. Do not invoke Keychain/Credential
Manager or request OS password approval; preserve old encrypted copies and restore available setup. This Mac remains
authorized for the signed update.

Testing handoff: `docs/HANDOFF.md`, with the code map in `docs/HANDOFF_AST.md`. Release notes: `docs/RELEASE_NOTES.md`.

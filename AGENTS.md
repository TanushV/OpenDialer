# Working agreement

Testing handoff (current): `docs/HANDOFF.md`, with the code map in `docs/HANDOFF_AST.md`.
Active feature plan: `docs/CAMPAIGN_PREP_PLAN.md` (one Markdown script, per-lead agent preparation, simple settings; implemented in 0.7.0). Script learning: `docs/SCRIPT_LEARNING.md`.

The active plan is `docs/LEAN_PLAN.md` (approved October 22, 2026). Read it in full on every continuation or context compaction, including its Status section, which is the current checkpoint. It keeps every end feature and removes hardening, legacy upgrade code, dead code, duplicate mechanisms and acceptance harnesses. `docs/REMEDIATION_PLAN.md`, `docs/EXECUTION_SPEC.md`, `docs/EXECUTION_TASKS.json` and `docs/STATUS.md` are history: consult them for what a feature does, not for mechanisms or order. Preserve both modes: standalone local (SQLite) and the shared hosted server (PostgreSQL).

Write acceptance tests before changing code and keep them green on SQLite and PostgreSQL. Add a regression test before fixing a defect. Commit coherent steps often. Never commit credentials, app databases, recordings, build products, or test caches. Never add AI attribution to commits, PRs or releases.

Tests with simulated providers are evidence about software behavior only. Carrier calls, hardware audio, production hosting, account eligibility, legal approval, and outcome lift require their own evidence. Keep those gates explicit; never convert an absent API key into a successful integration claim.

October 30 owner authorization: protect saved credentials, verify and publish the current release, and update this Mac through the signed updater. Preserve installed workspace data. Future installs/updates still require an explicit owner request.

October 30 follow-up: desktop credentials use an automatic private local key file. Do not invoke Keychain/Credential Manager or request OS password approval; preserve old encrypted copies and restore available setup. This Mac remains authorized for the signed update.

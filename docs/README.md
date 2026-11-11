# Documentation

## Start here

1. [Install and update](INSTALL.md)
2. [Setup guide](GETTING_STARTED.md): from a fresh install to your first call, step by step
3. [Example script](examples/script.md) and [example lead list](examples/leads.csv) to copy from
4. [What's new](RELEASE_NOTES.md)

## Use OpenDialer

- [CPU, RAM and platform requirements](REQUIREMENTS.md)
- [Provider accounts: Telnyx, ChatGPT, OpenRouter](../deploy/runbooks/providers.md)
- [Team server and direct webhook forwarding](../deploy/runbooks/deploy.md)
- [Backups, restore and call recovery](../deploy/runbooks/recovery.md)
- [Security and data handling](policies/security.md)

## Develop and operate

- [Architecture and calling-policy review](ARCHITECTURE.md): diagrams, current behavior, requested cadence and warmup, learning loops, and feature gaps
- [Calling completion plan](history/CALLING_COMPLETION_PLAN.md): November 2 scope, optional screening, manual calling, follow-up scripts, live conversation helper and setup fixes
- [Development and tests](DEVELOPMENT.md)
- [Current verification and remaining live gates](VALIDATION.md)
- [2.0.0 handoff: open issues and everything left to test](HANDOFF_2.0.md)
- [Current handoff](HANDOFF.md) and [code map](HANDOFF_AST.md)
- [Release procedure](../deploy/runbooks/releases.md)
- [Script learning and memory](SCRIPT_LEARNING.md)
- [Optimization](OPTIMIZATION.md)

## Plans and history

[LEAN_PLAN.md](history/LEAN_PLAN.md) is the active engineering plan; its Status section records progress.
[CAMPAIGN_PREP_PLAN.md](history/CAMPAIGN_PREP_PLAN.md) records the 0.7 design: one Markdown script, per-lead preparation and
one-page Settings. `history/REMEDIATION_PLAN.md`, `history/EXECUTION_SPEC.md`, `history/EXECUTION_TASKS.json`, `history/STATUS.md` and older evidence
are historical. For how to use the app, follow the guides above, not old plans or verification snapshots.

# OpenDialer

A desktop calling workspace for individuals and small sales teams. Import leads, read your script,
call the next eligible person, get live coaching and review follow-ups in one app.
Run privately on your computer or connect to a shared team server.

[Download](https://github.com/TanushV/OpenDialer/releases/latest) · [Install](docs/INSTALL.md) · [Set up](docs/GETTING_STARTED.md) · [Documentation](docs/README.md)

## Install

| Computer | Download | Install |
| --- | --- | --- |
| Apple Silicon Mac, macOS 14+ | [Mac installer](https://github.com/TanushV/OpenDialer/releases/download/v0.5.1/OpenDialer_0.5.1_aarch64.dmg) | Open the DMG and drag OpenDialer to Applications. |
| Windows PC, Intel/AMD 64-bit | [Windows installer](https://github.com/TanushV/OpenDialer/releases/download/v0.5.1/OpenDialer_0.5.1_x64-setup.exe) | Run the setup EXE. WebView2 is included. |

No Python, Node, Rust or separate database is needed to use the desktop app.
Intel Mac, Windows ARM and Linux desktop installers are not published.
This is an internal-use preview: updater signatures are configured, but Apple notarization and Windows publisher signing are not. See [installation help](docs/INSTALL.md).

**Release status:** the published download is **0.5.1**. `main` also contains newer setup fixes and
explicitly approved number purchasing; those changes are **not yet in that download**.

## Get started

1. Open the app and start a local workspace, or connect to your team's server.
2. Follow **Settings → Setup** to connect calling and AI, then set your business details, campaign policy and budgets.
3. Add eligible phone numbers, import leads and upload your Markdown script in **Intelligence**.
4. Run preflight, check your audio and go ready. Press **Space** or **Next call** to call the scheduler's next eligible lead.

[The setup guide](docs/GETTING_STARTED.md) explains the keys, phone numbers, callbacks and first call.
Local calling needs a reachable HTTPS webhook route; the guide includes that step.

## Hardware

| Use | CPU recommendation | RAM recommendation |
| --- | --- | --- |
| One desktop user | Apple Silicon, or a modern Intel/AMD CPU with 4 cores | 8 GB; 16 GB for heavy multitasking |
| Small shared server | Start with 4 vCPU and measure your team's workload | Start with 8 GB |

These are **planning recommendations, not certified minimums**. No dedicated GPU is needed:
AI requests go to remote providers; statistical training runs on the CPU.
Allow 2 GB free space initially, plus growing workspace data and backups. Use a microphone/headset,
stable internet and a display with at least 1024 × 720 usable pixels.
[Platform details and measured resource usage](docs/REQUIREMENTS.md).

## What it does

- Scheduler-selected calls and retries, readable scripts and live helper tips.
- Post-call summaries and outcomes with human corrections, follow-ups and focused practice.
- Script memory, statistical learning, number allocation and capacity planning.
- Calling policies, opt-outs, DNC checks, costs and budgets.

Phone-number purchases require explicit quote approval and configured limits in current source.
Allocation uses ready inventory; buying or syncing a number does not start warmup.
There is no call recording or prerecorded voicemail drop. Raw speech and helper text expire within five minutes.
Provider keys are stored as plain JSON on the local computer or team server; protect that machine and its backups.

## Help and development

- [Installation and updates](docs/INSTALL.md)
- [Setup and daily calling](docs/GETTING_STARTED.md)
- [Team deployment](deploy/runbooks/deploy.md) · [Backup and recovery](deploy/runbooks/recovery.md)
- [Development and tests](docs/DEVELOPMENT.md) · [Verification status](docs/VALIDATION.md)
- [Report a problem](https://github.com/TanushV/OpenDialer/issues)

Automated tests cover software behavior. Live carrier calls, hardware audio, provider eligibility and production capacity need separate verification.

[Apache-2.0](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)

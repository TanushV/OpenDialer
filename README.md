# OpenDialer

A desktop calling workspace for individuals and small sales teams. Import leads, read your script,
call the next eligible person, get live coaching and review follow-ups in one app.
Run privately on your computer or connect to a shared team server.

[Download](https://github.com/TanushV/OpenDialer/releases/latest) · [Install](docs/INSTALL.md) · [Set up](docs/GETTING_STARTED.md) · [Documentation](docs/README.md)

## Install

| Computer | Download | Install |
| --- | --- | --- |
| Apple Silicon Mac, macOS 14+ | [Mac installer](https://github.com/TanushV/OpenDialer/releases/download/v0.6.2/OpenDialer_0.6.2_aarch64.dmg) | Open the DMG and drag OpenDialer to Applications. |
| Windows PC, Intel/AMD 64-bit | [Windows installer](https://github.com/TanushV/OpenDialer/releases/download/v0.6.2/OpenDialer_0.6.2_x64-setup.exe) | Run the setup EXE. WebView2 is included. |

No Python, Node, Rust or separate database is needed to use the desktop app.
Intel Mac, Windows ARM and Linux desktop installers are not published.
This is an internal-use preview: updater signatures are configured, but Apple notarization and Windows publisher signing are not. See [installation help](docs/INSTALL.md).

**Version 0.6.2** includes encrypted credentials, shared webhook setup, Codex login import and list/state number planning.

## Get started

1. Open the app and start a local workspace, or connect to your team's server.
2. Follow **Settings → Setup** to connect calling and AI, then set your business details, campaign policy and budgets.
3. Add eligible phone numbers, import leads and upload your Markdown script in **Intelligence**.
4. Run preflight, check your audio and go ready. Press **Space** or **Next call** to call the scheduler's next eligible lead.

[The setup guide](docs/GETTING_STARTED.md) explains the keys, phone numbers, callbacks and first call.
**Shared webhook setup:** import your per-computer setup file in Settings → Setup → Calling. It supplies the common Supabase webhook and carrier IDs, with no tunnel or terminal command. Audio goes directly to Telnyx; business data stays local. [Setup details](docs/GETTING_STARTED.md#local-calling-with-the-shared-webhook).

**Using your ChatGPT plan?** Choose **Settings → Setup → AI assistance →
ChatGPT via Codex · import login**, then select your Codex `auth.json` file. The bundled runtime
refreshes the login automatically while in use. [Get the file and connect](docs/GETTING_STARTED.md#connect-your-codex-login).

## Hardware

| Platform | Minimum status | Comfortable starting point |
| --- | --- | --- |
| Mac | Apple Silicon, macOS 14+. An 8 GB M1 has not yet been performance-tested. | **8 GB RAM** for focused use; more for demanding multitasking. |
| Windows | x64 PC. Windows 11's 2-core / 4 GB OS floor is **not a verified app minimum**. | **8 GB RAM** provisionally; no demonstrated 4-core requirement for calling. |
| Team server | No certified minimum or rep capacity yet. | Benchmark your team workload before choosing CPU/RAM. |

Comfortable figures are provisional. **16 GB is not an established OpenDialer requirement.**
The expanded component test measured about 182 MiB at fresh idle and an 865 MiB peak with training;
that excludes the native shell and optional Codex runtime and does not certify low-RAM hardware. Latency targets were not all met on the loaded test Mac.
No dedicated GPU is needed: AI inference is remote, and statistical training uses the CPU.
Allow 2 GB free disk initially plus data/backups, a microphone/headset, stable internet and
at least 1024 × 720 usable display pixels.
[Measured usage, test method and remaining hardware checks](docs/REQUIREMENTS.md).

## What it does

- Scheduler-selected calls and retries, readable scripts and live helper tips.
- Post-call summaries and outcomes with human corrections, follow-ups and focused practice.
- Script memory, statistical learning, number allocation and capacity planning.
- Calling policies, opt-outs, DNC checks, costs and budgets.

Phone-number purchases require explicit quote approval and configured limits.
Plan numbers by named lead list, primarily by state, with suggested metro area codes for new rentals.
Allocation reuses ready numbers across that state and preserves existing caller identities;
buying or syncing a number does not start warmup. See [list-based number setup](docs/GETTING_STARTED.md#numbers-for-a-lead-list).
There is no call recording or prerecorded voicemail drop. Raw speech and helper text expire within five minutes.
Saved credentials are encrypted using an automatically created private local key file, with no Keychain or Credential Manager prompts; team servers use a separately supplied encryption key. Settings remembers them across restarts. Protect the machine and business-data backups. [Security details](docs/policies/security.md).

## Help and development

- [Installation and updates](docs/INSTALL.md)
- [Setup and daily calling](docs/GETTING_STARTED.md)
- [Team deployment](deploy/runbooks/deploy.md) · [Backup and recovery](deploy/runbooks/recovery.md)
- [Development and tests](docs/DEVELOPMENT.md) · [Verification status](docs/VALIDATION.md)
- [Report a problem](https://github.com/TanushV/OpenDialer/issues)

Automated tests cover software behavior. Live carrier calls, hardware audio, provider eligibility and production capacity need separate verification.

[Apache-2.0](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)

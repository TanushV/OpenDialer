# OpenDialer

A desktop dialer for one rep per computer, on Mac, Windows or Linux. Import a CSV or sync a HubSpot list, paste your call script
once, and an AI agent researches each lead and writes that lead's version of your script. Press Start calling and the
app dials lead after lead, skips voicemail, shows the script and live tips while you talk, and writes a summary with a
suggested outcome when the call ends.

[Download the latest release](https://github.com/TanushV/OpenDialer/releases/latest) · [Setup guide](docs/GETTING_STARTED.md) ·
[How it works](docs/ARCHITECTURE.md) · [What's new](docs/RELEASE_NOTES.md) · [Example script](docs/examples/script.md)

## Install

| Computer | From the latest release | Install |
| --- | --- | --- |
| Apple Silicon Mac, macOS 14 or newer | `OpenDialer_<version>_aarch64.dmg` | Open it and drag OpenDialer to Applications. |
| Windows PC, 64-bit | `OpenDialer_<version>_x64-setup.exe` | Run it and follow the prompts. Windows may warn once: the installer is not publisher-signed yet. |
| Ubuntu 24.04 LTS, 64-bit Intel or AMD | `OpenDialer_<version>_amd64.deb` | Install the downloaded file with `sudo apt install ./OpenDialer_<version>_amd64.deb` and open OpenDialer from the app menu. |

The app updates itself: **Settings → More settings → Workspace and app updates → Check for updates**. Your leads,
script, numbers and saved keys are kept across updates.

## Set up once

Settings opens with one row per thing to set. A row turns green only after a real check.

| Row | What to do |
| --- | --- |
| Telnyx API key | Paste it from the Telnyx portal. Calls, numbers and transcription run on your Telnyx account. |
| Call connection | Import your calling setup file once. After that it reconnects by itself. |
| Phone numbers | In Numbers, buy a number or add one you own. A new number starts at 15 calls a day and rises to 60. |
| ChatGPT login | Run `codex login` in Terminal, then use this computer's login. Research and scripts run on your plan. |
| OpenRouter key | Live tips during calls and the after-call summary, on a no-retention route. |
| Test phone | Your own number. **Test call** dials it right away, at any hour. |
| HubSpot, Treg, RealValidito | Optional: HubSpot lists, number checks and do-not-call checks for HubSpot leads. |

## Call

1. **Script:** paste your Markdown script and click **Save script**. The page says whether your changes are saved.
2. **Leads:** import a CSV or sync a HubSpot list. Each lead is researched and gets its own script.
3. **Dialer:** while paused it says how many leads can be called now, or why none can. Press **Start calling** (Space).
   **P** pauses after the call. When a call ends, press **1** to **6** to save the outcome, or Enter to accept the
   suggested one. The next call starts by itself.
4. **Review:** opens with what needs you and the one thing to do about each, then follow-ups, past calls and what the
   app has learned about your best hours and triggers.

## Calling does not stop by surprise

Everything that can stop a call has a named reason and a way out: it either clears by itself within a stated time, or
the screen shows the control that fixes it. If something that should clear by itself does not, the app repairs it,
and if that fails it shows **Reconnect** instead of waiting. These rules are checked in CI over every combination of
inputs and thousands of randomized runs. See [How it works](docs/ARCHITECTURE.md).

## Good to know

- No call recordings and no prerecorded voicemail drop. Transcripts stay on this computer for 30 days, then are
  deleted; delete one sooner from Review. An opt-out deletes that call's transcript.
- Saved keys are encrypted on this computer, with no Keychain or password prompts.
- Automated tests cover software behavior. Real carrier calls, audio hardware and provider accounts need your own check.
- Hardware: Apple Silicon Mac, 64-bit Windows or Ubuntu 24.04 LTS x86-64, 8 GB RAM, a headset and a stable connection.

## More

[Setup guide](docs/GETTING_STARTED.md) · [How it works](docs/ARCHITECTURE.md) · [Development and tests](docs/DEVELOPMENT.md) ·
[Report a problem](https://github.com/TanushV/OpenDialer/issues)

[Apache-2.0](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)

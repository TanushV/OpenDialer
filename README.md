# OpenDialer

A desktop app for calling leads one at a time. You paste your call script once, import a list, and AI agents
research every lead and write that lead's own version of your script before you call. While you talk, a live
helper suggests what to ask next. After the call you get a summary, follow-ups and a short list of what to
practice.

[Download](https://github.com/TanushV/OpenDialer/releases/latest) · [Install](docs/INSTALL.md) ·
[Setup guide](docs/GETTING_STARTED.md) · [Example script](docs/examples/script.md) ·
[Example lead list](docs/examples/leads.csv)

## Install

| Computer | Download | Install |
| --- | --- | --- |
| Apple Silicon Mac, macOS 14 or newer | [Mac installer (.dmg)](https://github.com/TanushV/OpenDialer/releases/download/v0.7.1/OpenDialer_0.7.1_aarch64.dmg) | Open it and drag OpenDialer to Applications. |
| Windows PC, Intel/AMD 64-bit | [Windows installer (.exe)](https://github.com/TanushV/OpenDialer/releases/download/v0.7.1/OpenDialer_0.7.1_x64-setup.exe) | Run it and follow the prompts. |

Already installed? Open **Settings → More settings → Workspace and app updates → Check for updates**.
Nothing else needs installing. This is a preview: it is not Apple-notarized or Windows publisher-signed,
so your computer may ask you to confirm the first open. See [installation help](docs/INSTALL.md).

## Get started in six steps

| Step | Where | What you do |
| --- | --- | --- |
| 1. Open a workspace | First screen | **Start a local workspace** (just you, data stays on this computer) or **Connect to a server** (your team's). |
| 2. Connect services | **Settings** | **Calling:** import your calling setup file or paste your Telnyx key. **AI:** click **Use the ChatGPT login on this computer**, then save the live help key (OpenRouter). |
| 3. Set the rules | **Settings** | **Calling hours:** set the hours, confirm your business, tick **Approved for live calls**. **Do-not-call list:** load it for the area codes you call. |
| 4. Add your script | **Script** | Paste your Markdown script (start from the [example](docs/examples/script.md)) and click **Save script**. |
| 5. Import leads | **Leads** | Name the list, paste or choose a CSV with `name` and `phone`, click **Import leads**. Agents research each lead and write its script; the panel shows progress. |
| 6. Call | **Dialer** | Get phone numbers ready in **Numbers**, click **Check readiness** in Settings, then **Go ready** and press **Space** for the next call. |

The [setup guide](docs/GETTING_STARTED.md) walks through every step, including phone numbers, the script format
and what to do when something is blocked.

## Your script, in one minute

```markdown
# Opening
- Hi {first_name}, this is {rep} with {seller}. {{ why we're calling {company} now, from research }}

# Screening
{rep} from {seller}, calling about customer follow-up.

# Discovery
- How do you follow up with customers today?
- Who else weighs in on changes to that process?

# Close
- Would a 15-minute call later this week be useful?
```

- `# Opening`, `# Screening`, `# Discovery` and `# Close` are required. `# Gatekeeper`, `# Offers`,
  `# Objections`, `# Facts` and `# Never say` are optional.
- Each bullet is an option. The AI picks the best ones for each lead.
- `{{ … }}` is a line the AI writes for each lead from research, following your instruction.
- `{first_name}`, `{name}`, `{company}`, `{rep}` and `{seller}` are filled in for you.

## What each page is for

| Page | Use it to |
| --- | --- |
| **Dialer** | Call the next lead, read their script, get live tips, record the outcome. |
| **Leads** | Import lists, watch research progress, open any lead's script and research. |
| **Review** | See what to practice and how, your goals, follow-ups and past calls. |
| **Numbers** | Bring in or buy caller numbers and get each one ready to use. |
| **Script** | Edit your script, see what is working, and describe what you sell. |
| **Settings** | Connect services and set calling rules. Everything else is under **More settings**. |

## What it costs to run

- **Research and scripts** run on your ChatGPT plan (GPT-6.1 Terra), included in the plan.
- **Live help and call notes** use your OpenRouter key (Cerebras). An **AI budget per lead list** in
  More settings caps that spend; it never blocks calling.
- **Calls and numbers** are billed by Telnyx. Numbers are only bought after you approve an exact quote.

## Good to know

- There is no call recording and no prerecorded voicemail drop. Raw speech and helper text are deleted
  within five minutes.
- Saved keys are encrypted on your computer; no Keychain or password prompts.
- Automated tests cover software behavior. Real carrier calls, audio hardware and provider accounts need
  your own check before you rely on them.
- Hardware: Apple Silicon Mac or 64-bit Windows, 8 GB RAM recommended, a headset and a stable connection.
  [Measured requirements](docs/REQUIREMENTS.md).

## More help

- [Install and update](docs/INSTALL.md) · [Setup guide](docs/GETTING_STARTED.md) · [What's new](docs/RELEASE_NOTES.md)
- [Provider accounts](deploy/runbooks/providers.md) · [Team server](deploy/runbooks/deploy.md) ·
  [Backup and recovery](deploy/runbooks/recovery.md) · [Security](docs/policies/security.md)
- [Development and tests](docs/DEVELOPMENT.md) · [All documentation](docs/README.md)
- [Report a problem](https://github.com/TanushV/OpenDialer/issues)

[Apache-2.0](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)

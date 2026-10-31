# Simple setup and per-lead campaign preparation (implemented in 0.7.0)

Owner request, October 30, 2026; approved the same day with these changes: OpenAI web search instead of DDGS, the
AI budget per lead list, real model runs for testing (the owner's Codex login and the OpenRouter key saved in the app),
no real phone calls.
This plan sits under [LEAN_PLAN.md](LEAN_PLAN.md) (both database modes, tests first, no AI attribution) and
changes the script and preparation parts of [SCRIPT_LEARNING.md](SCRIPT_LEARNING.md).

## Goal

Hyper simple to start: paste one Markdown script, import a lead list, let AI agents research every lead and write
that lead's script up front, then call. Settings shrink to what an owner must set. Clutter and jargon go.

## What the code does today (read October 30)

- Three places hold script text: Intelligence "Your script" (Markdown, `templates.import_markdown`), Settings >
  Operations "Script and advance preparation" (typed template fields plus 8 preparation numbers,
  `Preparation.tsx`), and Product facts (a second opener and screening line, `ProductKnowledge.tsx`).
- Per-lead "script writing" fills two slots only, `{relevance}` and `{proof}` (`templates.fill`,
  `intelligence.prepare_one`). A pasted Markdown script without those placeholders is not personalized at all.
- Research reads only the imported `domain` (`_research_company`); no domain means no research. Search is the
  metered OpenAI web search, API-key route only (`InferenceClient.search`). Facts are keyed by company domain.
- CSV import keeps a fixed set of columns and drops the rest (`service.import_leads`).
- Preparation is a rolling target (20 per staffed rep, capped at 100) inside an off-hours window
  (`preparation/workflows.py`), not the whole list up front.
- Codex route: one-shot `thread/start` with a single submit tool, `web_search = "disabled"`, no effort setting,
  model `gpt-6.1-sol` (`providers/codex.py`, `config.py`). A second ChatGPT route (`chatgpt_plan`, OAuth to the
  Responses endpoint, `providers/oauth.py`) duplicates it.
- Every optional AI request reserves $0.10 against the AI budget before running, including the ChatGPT routes,
  whose usage is later booked at $0 (`accounting.usage`). A $0 budget blocks free plan work.
- OpenRouter is used only for the live helper and practice (Cerebras, `configured_coach`). Research, script
  writing, meaning reviews and post-call analysis use the selected OpenAI/ChatGPT route.
- The queue knows a company grouping (`scheduling.models.company_group`) but uses it only for evaluation. Nothing
  spaces out contacts at one company, and a booking at one contact does not stop calls to the others
  (`rep_workflows._meeting_in` holds only that lead).
- Script learning keys sections by the old template fields (`script_learning.SECTIONS`) plus `sections:i`.
- Settings: 4 tabs, a 9-step checklist, versioned policy edits that each need a "publication reason", registry
  import, SSH forward, capability evidence, scheduled prepaid DNC screening (`compliance/screening.py`).
- Coaching on Review has five blocks: patterns, observed groups, goals, opportunities, examples.

## Decisions taken with the owner

- Keep metered OpenAI web search for the API-key route. On the ChatGPT route, research uses Codex's own web
  search, which is plan usage. No DDGS.
- ChatGPT route model: `gpt-6.1-terra` at reasoning effort `max`, for every ChatGPT-route role.
- OpenRouter stays Cerebras-only for live help and practice.
- "Multithreaded" means several contacts at one company: never call them back to back, and once one books, stop
  calling the others.
- Scheduled prepaid screening is removed. The manual prepaid check stays.

## 1. One script, one Markdown structure

The script is pasted or uploaded once, on the Script page. It is parsed as you type; errors show inline under the
editor; there is no preview step. Allowed top-level headings, in any order:

| Heading | Required | Meaning |
|---|---|---|
| `# Opening` | yes | What the rep says first. Bullets make it a set of options (one is picked per lead). |
| `# Screening` | yes | Line for iPhone call screening; must contain `{seller}`. Fixed. |
| `# Gatekeeper` | no | Line for a gatekeeper. Fixed or options. |
| `# Discovery` | yes | Bullets: question options. Up to 3 are picked and ordered per lead. |
| `# Offers` | no | `##` per offer. One lead offer and one backup are picked per lead. |
| `# Objections` | no | `##` per objection. All are kept, ordered by likelihood for this lead. |
| `# Close` | yes | The ask. Bullets make it options (one picked). |
| `# Facts` | no | Bullets: true claims the rep and AI may use. Replaces Product facts claims. |
| `# Never say` | no | Bullets: banned claims. Replaces prohibited claims. |

Inside any section:
- `{name}`, `{first_name}`, `{company}`, `{rep}`, `{seller}` are filled from the lead and workspace.
- `{{ instruction }}` is a block the AI writes for each lead, for example
  `{{ one sentence on why this matters to them now, from research }}`. A section can be only an AI block.
- Everything else is the seller's wording and is never changed.

Unknown headings, a missing required heading, a Screening line without `{seller}`, an empty option, an unknown
placeholder or an unclosed `{{` are errors with the line number. The parser produces the template the rest of the
app already uses (sections, options with stable ids, AI blocks with ids). The typed template editor, the
preparation fields and the opener/screening fields in Product facts are deleted. Product facts keeps only a plain
"What we sell" description for the live helper; claims and banned claims come from the script.

## 2. Deep research and script writing by agent threads, before calling

- **Start**: importing a list (or pressing "Prepare list") queues the whole list. Leads are grouped into threads
  of X leads each (owner setting "Leads per agent", default 5, 1 to 20). Up to Y threads run at once ("Agents at
  once", default 2, 1 to 4). Leads of one company go to the same thread so research is shared.
- **Context**: each lead's whole CSV row is kept on import (all columns, capped at 60 columns and 8 KB) and given
  to the agent, plus the parsed script, Facts, Never say, the campaign goal and the script memory digest.
- **One thread, two phases** (Codex app-server, `gpt-6.1-terra`, effort `max`, web search live; API route uses the
  Responses API with the same tools and metered web search):
  1. Research. Tools: built-in web search; `save_facts(lead_id, facts)` where each fact has a statement, source
     URL and an exact quote. The app fetches the URL with the existing safe fetcher and rejects a fact whose quote
     is not on the page, or whose page does not name the company or person. `finish_research(lead_id, summary)`
     closes research for a lead, with "nothing usable found" allowed.
  2. Script writing. `submit_script(lead_id, picks, written)`: option ids for each options section, text for each
     AI block with the fact ids it relies on. Refused until that lead's research is closed. `skip_lead(lead_id,
     reason)` for a wrong number, closed business or similar.
- **Close-out requirements** enforced by the tools, which return precise errors so the agent fixes and resubmits:
  every attached lead ends with an accepted script or a skip; every AI block is filled and within its length
  limit; option ids exist and counts match the rules in section 1; every factual sentence cites saved facts for
  that lead; no Never say phrase; no price, number or guarantee that is not in Facts or a cited fact; `{seller}`
  identity lines unchanged. Written text that is not a verbatim fact gets the existing meaning review
  (`grounding.review_generated`). If a turn ends with leads still open, the thread gets up to 2 more turns
  listing them; after that they are marked failed and use the base script.
- **Plan limits**: a ChatGPT usage-limit error pauses all threads and shows when work resumes; it never marks
  leads failed. Work already accepted is kept. Restarting the app resumes queued threads.
- **Calling while preparing**: the queue serves only leads whose preparation finished (accepted, skipped or
  failed after retries). Calling can start as soon as the first thread closes; no unprepared lead is dialed.
- **Freshness**: a script change, a Facts/Never say change or a changed row re-queues the affected leads. Facts
  keep the existing stable / time-sensitive expiry, now fixed constants instead of settings.
- **Cost**: ChatGPT-route work makes no budget reservation and books $0. The API route keeps reservations and
  budget stops. The rolling-target settings (ahead count, caps, off-hours window, fact cache days, batch size,
  timezone) are removed.

## 3. Multithreaded accounts (several contacts at one company)

- Company = the existing `company_group` (CSV company id, then domain, then company name).
- Spacing: after any call to a contact at a company, other contacts there are not offered until the next calling
  day in the prospect's time zone. A callback a prospect requested is exempt. Applies across all reps.
- Booking: when any contact books (outcome or reported meeting), every other open contact at that company gets
  the hold "Company booked" naming who booked. Correcting or retracting that meeting removes the hold. Holds show
  on the lead and can be released by hand.

## 4. Settings: one page

No tabs. Top: five cards, each showing done or one action:
1. Calling: Telnyx key and connection (managed connection status stays).
2. AI: "Sign in with ChatGPT" (Codex app-server browser login, no auth.json import) or an OpenAI API key.
   Live helper key (OpenRouter) on the same card.
3. Script: status and a link to the Script page.
4. Leads: count, preparation progress, link to Leads.
5. Calling hours: start, end, attempts, and one "Approved for live calls" checkbox. No publication reason or
   reference fields; the app records "Edited in Settings" as the reason.

Below, one collapsed "More settings" list with plain one-line descriptions: preparation agents (X and Y), DNC
registry import and manual prepaid check, budget (API route only), rep schedule, callback routing, rubrics,
backups, billing reconciliation, learning experiments, trackers, SSH forwarding, work time, app updates,
workspace connection. Removed outright: scheduled prepaid screening (UI, job, policy fields, spend tracking,
tests), the business identity text field (replaced by a single confirmation checkbox), the capability evidence
block, the `chatgpt_plan` OAuth route (superseded by ChatGPT sign-in through Codex). All copy is rewritten in short
plain sentences without internal terms (authority, ingress, quiescence, capability, ZDR, cohort).

## 5. Pages after the change

- **Script** (renamed from Intelligence): the editor and live parse view (sections, option counts, AI blocks
  highlighted as "written per lead"); below it, collapsed "What's working" (script memory and tests) and "Stats".
- **Leads**: import (any columns), then a preparation panel: overall progress, one row per running agent
  ("Agent 2: writing scripts, 3 of 5"), ready / skipped / failed counts, pause state. A lead opens to its research
  facts with sources and its written script.
- **Dialer**: the lead's own script. Picked opening, top questions, lead offer (backup collapsed), objections in
  likelihood order, AI-written lines marked subtly. Live helper unchanged in behavior, now given the lead's script
  and picks.
- **Review**: coaching is one list. Each item: what to improve, one example, and a Practice button. Goals and
  reviewed examples become items in the same list.
- Visual pass across these pages: consistent heading sizes, spacing, one primary button per card, fewer pills.

## 6. Script learning follows the new structure

Section keys become the Markdown sections plus option ids (`offers:free-inspection`, `discovery:q3`). The per-call
snapshot records the picks, so learning can report which offers and questions do better, as correlations with the
existing thresholds. Proposals and tests edit the base Markdown sections that are not locked; Screening and
identity stay locked.

## 7. Tests (written before each change, SQLite and PostgreSQL)

- Parser: every heading rule, options, AI blocks, placeholders, `{seller}` lock, line-numbered errors.
- Import keeps the full row within caps.
- Preparation: threads of X, Y at once, same company in one thread, two-phase order, every tool rejection,
  skip, two extra turns then failure, plan-limit pause and resume, restart resume, no duplicate work, re-queue on
  script or row change, no reservation on the ChatGPT route, reservation on the API route.
- Queue: only prepared leads; company spacing across reps; requested callbacks exempt; booking holds siblings;
  correction releases them.
- Codex runtime: Terra and `max` sent; web search enabled only for research threads; only offered tools accepted;
  native test against the local fake model endpoint covering a multi-tool loop.
- Removed features return 404 or are absent; scheduled prepaid never queues.
- Live helper: receives the lead script and picks; stays Cerebras-only; existing helper suites stay green.
- Browser (local and hosted): setup page, script paste with errors, import and preparation progress, dialer
  script view, coaching list, More settings. Old specs for removed UI are rewritten, not deleted without
  replacement.

## 8. Real evidence (separate from simulated tests)

- ChatGPT sign-in, research and script writing on a small real list through the owner's ChatGPT plan.
- Live helper against real Cerebras only with an OpenRouter key; none is configured on this Mac today.
- Real calls need an owned number (none today; purchase needs explicit approval) and a test destination.
- Computer-use user journeys on a disposable dev workspace first; the installed workspace is only checked after
  the signed update, without changing its data unless the owner asks.

## 9. Release

Version 0.7.0: commits per step, full suites, push, CI, signed release, update this Mac through the signed
updater (owner request, October 30), verify the installed app opens and its data counts are unchanged.

## Risks

- Codex 0.164.0's built-in catalog lists `gpt-6.1-sol` and `gpt-5.6-terra` but not `gpt-6.1-terra` (released
  October 26). Step one checks the live model list and upgrades the pinned runtime if needed.
- ChatGPT plan usage limits will throttle large lists at `max` effort; the pause/resume path handles it, but a big
  list can take hours.
- Name-based research can match the wrong company or person. Quote-on-page and name-on-page checks reduce it;
  they do not eliminate it.
- Importing the same Codex login the owner uses elsewhere can break refresh-token rotation; ChatGPT sign-in
  gives OpenDialer its own session instead.

## Status

- October 30: plan written and approved (budget per lead list; OpenAI web search kept; real model runs allowed).
- Implemented (backend `e5f208b`, live-help fixes `02c43c6`, UI and tests in the 0.7.0 commits):
  `preparation/templates.py` (Markdown parser, renderer, close-out rules), `preparation/agent.py` (tools, thread
  handler, simulated agent), `preparation/workflows.py` (list requests, company-grouped threads, publication,
  per-list status), `providers/codex.py` (multi-turn agent loop, Terra at `max`, live web search, plan-limit pause,
  shared-login session), `providers/inference.py` (API agent loop with metered web search), queue company spacing,
  booking holds, per-list budget, removal of scheduled prepaid screening and the plan OAuth route; one-page Settings,
  Script editor, Leads preparation panel, per-lead script reader, coaching list.
- Real-model evidence (no calls; fictional numbers): one ChatGPT-route thread on `gpt-6.1-terra`/`max` prepared three
  real public companies in 129 s with 14 live web searches and 13 tool calls; 4 submissions were rejected by the
  close-out rules and corrected by the agent; every fact's quote was verified on its page. The live helper on real
  Cerebras exposed five defects that simulated tests could not: OpenRouter has no Cerebras endpoint for
  `parallel_tool_calls` (every review and summary 404'd); Cerebras does not enforce forced tool schemas but does
  enforce strict response schemas; the model misnumbers the state version and cites card ids as speech while echoing
  the exact context fingerprint; a rejected explanation hid an approved question; practice used version 0. Each has a
  regression test that failed first. After the fixes, repeated real runs showed manual help and automatic tips in
  most runs (1.0–2.3 s), practice in most runs, and post-call rundowns produced; failures that remain are the
  grounding and review rules declining a tip, which is the intended behavior.

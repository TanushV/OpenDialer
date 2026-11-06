# Markdown playbooks, fresh rewrites and token usage — 0.10.1

Owner approved November 5, 2026. Implementation, publication, this Mac's signed update and replacing Agite's
saved script with the owner's supplied playbook are authorized. Preserve all workspace data and credentials.
Read this checklist along with LEAN_PLAN.md and NUMBER_CONNECT_PLAN.md after every compaction.

## Scope

- One deterministic Markdown parser and representation: familiar heading aliases, optional supporting sections,
  Pitch, Goal, Instructions, Preparation and preserved custom sections; named ## choices; ordinary bullets remain
  checklists except the existing question/choice convention. Blockquotes are non-spoken cues. Keep existing scripts valid.
- Workspace-derived identity/screening defaults; no mandatory screening/discovery section. Keep malformed fields,
  missing caller identity, empty options and unsafe structural ambiguity visible with line-number errors.
- New meaningful template content creates a new generation key. Per-lead generation starts from the new master,
  current verified facts and call history, without using the old personalized script. Formatting edits cost no AI work.
  Active calls retain their snapshots. Old jobs cannot publish over newer input; no stale personalized-text fallback.
- Separate Rewrite scripts (reuse current research; no automatic searches when sufficient) and Refresh research
  and rewrite, with list/lead scopes, a measured sample and a visible estimate for the remaining work.
- Persist provider usage for subscription and metered routes, generation and independent review, including reported
  usage from failed/cancelled work. Deduplicate cumulative updates; distinguish unknown from zero and exact shared
  thread totals from estimated per-lead allocations. Show input/output/cached/reasoning, model/route, work kind,
  master version, list and job; distinguish subscription inclusion, provider receipts and cost estimates.
- Replace only Agite's installed workspace script with the owner's complete playbook, converted into the accepted
  Markdown structure. Preserve its variants, objections, proof restrictions and instructions. The 60% figure remains
  conditional on approved measurement details, and editorial questions stay non-spoken. No Agite app-wide default.
- Test SQLite/PostgreSQL, browser/provider contracts and native installed UI. No new carrier call is needed.

## TODOs

- [x] Acceptance and provider/parser regressions first; record failing-before evidence.
- [x] Persistent token accounting and scoped usage reporting, including partial/unknown totals.
- [x] Flexible Markdown structure, aliases, defaults, notes, options and editor/reader/live-helper integration.
- [x] Fresh generation, research reuse/refresh, sample estimates, active-call/version safeguards and progress.
- [ ] Full SQLite/PostgreSQL, frontend/browser/native checks; fix discovered errors with regressions.
- [ ] Bump 0.10.0, commit/push, build both signed desktop packages, verify and publish.
- [ ] Update this Mac through the signed updater; verify data, provider setup, numbers and capacity remain.
- [ ] Replace Agite's script through the app, regenerate from it and verify real token receipts and version linkage.
- [ ] Commit final evidence and current handoff; close only when every item works.

## Checkpoint

Implementation began from clean main `9e5d058`, installed 0.9.2. New parser/HTTP/provider/native/UI regressions
cover aliases, optional sections, directions, fresh generation, research modes, cumulative/unknown/partial tokens and
metered settlement. Full local SQLite 867 and PostgreSQL 868 pass; subsequent provider/cost regressions pass.
Frontend 83 tests and typecheck pass. Browser and native release checks continue. The owner playbook converts to
13 KB, three pitch variants and eleven objections, with conditional proof/charging lines kept as directions.
No production script or installed application has changed yet. See [usage guide](SCRIPT_PLAYBOOK.md).
The wider number/connect-rate plan remains open for its independent live scenarios and traffic measurement.

The full browser continuation exposed a nullable human-note field crashing fresh preparation after a call.
A failing HTTP regression reproduced the stuck state; empty notes are now normalized. Three more regressions
preceded excluding paused, archived and opted-out contacts from automatic dispatch, claims and progress.
All 33 related journeys pass on both stores. The initial signed draft remains unpublished and is being replaced
with these runtime fixes; full CI/browser verification and native installation are still required.

Native 0.10.0 verification: published/installed from `a7b3434`; all six CI jobs pass (867 SQLite,
868 PostgreSQL, 182 browser, 83 frontend, 25 native/package, seven Rust). Signatures, executable hashes,
manifest, retained record IDs, both ready numbers, zero pending setup, capacity, providers and hours pass.
Agite master v3 is saved through the application's existing import handler after native file selection;
CUA subsequently verified v3, directions, all variants/objections, scoped token reporting and different grounded
wording for two real leads. First five independent reviews passed; later reviews hit the existing 120-second
completion limit at maximum effort. Failed receipts correctly retained unknown counters. A failing delayed-provider
regression precedes a longer deadline only for offline script review. 0.10.1 is required before close-out.
New batches are temporarily paused while that patch builds; preserve and restore the prior unpaused state,
resume remaining leads and regenerate any current-master scripts whose independent review was interrupted.

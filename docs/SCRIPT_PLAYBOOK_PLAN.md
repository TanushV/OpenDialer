# Markdown playbooks, fresh rewrites and token usage — 0.10.0

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
- [ ] Persistent token accounting and scoped usage reporting, including partial/unknown totals.
- [ ] Flexible Markdown structure, aliases, defaults, notes, options and editor/reader/live-helper integration.
- [ ] Fresh generation, research reuse/refresh, sample estimates, active-call/version safeguards and progress.
- [ ] Full SQLite/PostgreSQL, frontend/browser/native checks; fix discovered errors with regressions.
- [ ] Bump 0.10.0, commit/push, build both signed desktop packages, verify and publish.
- [ ] Update this Mac through the signed updater; verify data, provider setup, numbers and capacity remain.
- [ ] Replace Agite's script through the app, regenerate from it and verify real token receipts and version linkage.
- [ ] Commit final evidence and current handoff; close only when every item works.

## Checkpoint

Implementation beginning from clean main `9e5d058`, installed 0.9.2. Six new parser/HTTP cases fail before
product changes (work/script-playbook-red.log). Existing usage has OpenRouter receipts but no persisted ChatGPT preparation totals.
The wider number/connect-rate plan remains open for its independent live scenarios and traffic measurement.

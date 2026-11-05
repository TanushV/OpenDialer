# List management and local calling hours — 0.9.1

Owner approved November 5, 2026. This extends LEAN_PLAN.md with an end feature.

- New outbound calls stop at 17:00 in every applicable lead-local time zone, including callbacks.
  Existing conversations finish normally; inbound callbacks remain answerable. Earlier configured cutoffs stay.
- Lists show total, prepared/preparing, callable now, held, completed and due-callback counts.
- Dial from all active lists, one list or several. Due callbacks outside that selection are separate, explicit choices.
- Pause/resume and archive/restore lists and leads. Archive is reversible and retains history, suppression,
  notes, tasks and caller bindings. Paused lists stop ordinary outbound; a due callback can be chosen explicitly.
- Select leads in bulk to pause, resume, archive, restore, move between lists and correct state/time zone.
- One campaign contact per phone; reimporting into another list links membership instead of another calling history.
- List and lead changes are enforced at reservation and again at call submission. Scope changes never affect an
  already active call. Existing imported list labels work without a data cutover or a historical scan at startup.
- Keep SQLite and PostgreSQL and the existing list preparation/capacity flows.

## TODOs

- [x] Regression-first hours and HTTP acceptance journeys.
- [x] Backend list membership, management, queue scope, explicit callbacks and 17:00 limit.
- [x] Leads list controls and bulk actions; Dialer selection and outside callbacks.
- [x] Browser journeys for management and selected-list dialing.
- [ ] Full database, frontend, browser and native checks; fix regressions.
- [ ] Bump 0.9.1, commit/push and build signed Mac and Windows packages.
- [ ] Verify package signatures/manifests and publish 0.9.1.
- [ ] Computer-use review of the implemented UI and update authorized Mac; retain workspace/credentials.
- [ ] Commit release evidence and update handoff/current plans.

This checklist closes on verified software/release completion. The wider number/connect-rate plan remains open
for its separate live scenarios, carrier qualification and real-traffic measurement.

## Checkpoint

Initial regression failures and focused SQLite/PostgreSQL results are recorded in the working logs.
Two new browser journeys pass and all 82 frontend tests plus typecheck/build pass. Additional regressions
cover company spacing in list counts, shared preparation progress and stale capacity after list edits.
The 17:00 cutoff applies to new submissions; ongoing conversations keep their transcription/help path.
Broader exact-source verification and the signed 0.9.1 release are next.

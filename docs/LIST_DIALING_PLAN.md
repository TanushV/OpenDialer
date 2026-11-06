# List management and local calling hours — 0.9.1, planner follow-up 0.9.2

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
- [x] Bump 0.9.1, commit/push and build signed Mac and Windows packages.
- [ ] Verify package signatures/manifests and publish 0.9.1.
- [ ] Computer-use review of the implemented UI and update authorized Mac; retain workspace/credentials.
- [ ] Commit release evidence and update handoff/current plans.
- [ ] Fix mixed-time-zone slot scoring found in installed capacity planning; publish/install 0.9.2.

This checklist closes on verified software/release completion. The wider number/connect-rate plan remains open
for its separate live scenarios, carrier qualification and real-traffic measurement.

## Checkpoint

Regression-first tests reproduced the hours, list-management, scope and shared-membership gaps. Full source
suites pass 849 cases on SQLite and 850 on PostgreSQL; 82 frontend and 25 native/package plus seven Rust
tests pass. The full browser run passed 180 of 181: the new list fixture reused a helper contact's phone,
so correct deduplication returned zero new contacts. With a separate phone, all eight helper/list journeys
pass together; a fresh full CI run is in progress. No product change followed the signed builds.
0.9.1 is published from `4ec9268`, with all six CI jobs green on the test-only fixture correction `6b54ca3`
(181 browser journeys). It is installed through the signed updater; signatures, executable hashes and all
workspace IDs/counts match. The Lists page, one-list selection, retained scope, lead filter and bulk-action
menu work in the installed UI. All active lists and the all-leads view are restored afterward.
Both numbers remain ready with zero pending setup, the saved greeting remains and hours are 08:00–17:00.
Native capacity refresh exposed a separate pre-existing mixed-zone scoring error on the newly imported list.
A regression reproduces `ZoneInfoNotFoundError` before the one-line correction. 0.9.2 is the signed follow-up
so already-updated 0.9.1 installations can receive the fix normally; final verification remains open.

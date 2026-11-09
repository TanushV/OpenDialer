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
- [x] Full database, frontend, browser and native checks; fix regressions.
- [x] Bump 0.9.1, commit/push and build signed Mac and Windows packages.
- [x] Verify package signatures/manifests and publish 0.9.1.
- [x] Computer-use review of the implemented UI and update authorized Mac; retain workspace/credentials.
- [x] Commit release evidence and update handoff/current plans.
- [x] Fix mixed-time-zone slot scoring found in installed capacity planning; publish/install 0.9.2.

This checklist closes on verified software/release completion. The wider number/connect-rate plan remains open
for its separate live scenarios, carrier qualification and real-traffic measurement.

## Complete — November 5, 2026

Regression-first tests reproduced the hours, list-management, scope and shared-membership gaps. The initial
browser failure reused a helper contact's phone; correct deduplication returned zero new contacts. The test
fixture correction passes both journeys together and the full 181-case suite.
0.9.1 is published from `4ec9268`, with all six CI jobs green on the test-only fixture correction `6b54ca3`
(181 browser journeys). It is installed through the signed updater; signatures, executable hashes and all
workspace IDs/counts match. The Lists page, one-list selection, retained scope, lead filter and bulk-action
menu work in the installed UI. All active lists and the all-leads view are restored afterward.
Both numbers remain ready with zero pending setup, the saved greeting remains and hours are 08:00–17:00.
Native capacity refresh exposed a separate pre-existing mixed-zone scoring error on the newly imported list.
A regression reproduced `ZoneInfoNotFoundError` before the one-line correction. Signed 0.9.2 is published and
installed from `ad3356c`, so already-updated 0.9.1 installations receive the fix normally. All six CI jobs pass:
850 SQLite, 851 PostgreSQL, 181 browser, 82 frontend, 25 native/package and seven Rust tests. Both desktop
builds pass; checksums, deep strict signatures, public manifest and installed executable hashes verify.
The installed UI/service report 0.9.2; native capacity retry now shows **Capacity plan is current** at
16:18 Pacific. All 59 leads, two numbers, four calls and eleven tasks retain their IDs, provider setup,
greeting and 08:00–17:00 hours remain, and dialing is paused with all active lists selected. No real call or
new microphone session was opened for these changes. [Final receipt](evidence/lean/release-092-20261105.json).

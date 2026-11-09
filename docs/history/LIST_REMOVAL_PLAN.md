# List removal and script-refresh clarity — 1.0.1

Owner requested November 5, 2026: removing leads from a list and removing whole lists must work and be intuitive;
explain and verify what happens after script edits. This follows the 1.0.0 release and preserves workspace data,
credentials, opt-outs, call history, script history and both SQLite/PostgreSQL modes. Read with LEAN_PLAN.md,
LIST_DIALING_PLAN.md, SCRIPT_PLAYBOOK_PLAN.md and the full NUMBER_CONNECT_PLAN.md after compaction.

## Intended behavior

- Remove from list offers actual selected-lead memberships, prefills the filtered or sole list, and never silently
  interprets an empty field as an unrelated Unnamed list. Count actual changes; reject a wholly unrelated source.
- Remove list removes it from the normal Lists and Dialer views and excludes its sole-list leads from the default
  Leads view. Contacts shared with another active list remain. Use the existing reversible archive/restore state;
  show removed lists and leads explicitly so restoration is straightforward. Preserve all history and opt-outs.
- Normal Leads shows active memberships; Removed and All views retain access to removed/archived/unassigned
  contacts. Filtering, search, totals and pagination operate on the selected view, including beyond the first 50.
- Script save says whether a meaningful edit queues fresh lead scripts or formatting leaves current scripts alone.
  Show the current master and live preparation status on Script. Keep existing current-call snapshots, stale-job
  fences, research reuse, paused/removed exclusions and token accounting. Do not edit Agite's production script
  merely to demonstrate a refresh; use isolated simulated data for native mutation checks.

## TODOs

- [x] Add failing HTTP/browser regressions for removal, recoverable views and script-save refresh feedback.
- [x] Fix membership removal and accurate outcomes; expose explicit, reversible list and lead removal/restore.
- [x] Make filtered active/removed views correct across pagination and shared contacts on both stores.
- [x] Show and verify script-change refresh identity, progress and formatting-only behavior.
- [x] Pass affected and full checks; bump/commit/publish the signed 1.0.1 patch.
- [x] Update the authorized Mac, verify through computer use with isolated examples, retain production setup/data.
- [x] Commit final evidence/handoff and close only after all items pass. Keep wider carrier/measurement gates open.

## Completion checkpoint — November 5, 2026

Complete in published and installed 1.0.1, source `c3a12ea`. [Final receipt](evidence/lean/release-101-20261105.json).
All six CI jobs pass: 877 SQLite, 878 PostgreSQL, 187 browser, 83 frontend, 25 native/package and seven Rust tests.
Both signed builds pass; the Mac job recovered a transient hdiutil Resource busy error without a source change.
The first full browser run passed 186/187; its one broad save-status locator matched the new persistent status and
its toast. Scoping it to the editor fixed the test; four local cases and the fresh full CI pass.

Failing regressions preceded invalid-source/no-op membership removal, Active/Removed filtering across 50-row
pagination, shared-contact preservation, script-save refresh identity/progress, removed preparation/capacity choices,
concurrent duplicate import and newly imported-row visibility fixes. Reuse existing archive state; no schema or
physical deletion path was added. The superseded candidate remains unpublished.

This Mac updated through the signed updater. Native UI/service report 1.0.1; archive/public manifest/checksums,
bundle signature, release source tag and all five installed executables verify. Native tests connected the installed
app to a temporary localhost simulation served by its signed backend: import, membership removal/recovery,
whole-list removal/restoration, shared-contact preservation, automatic fresh scripts and formatting-only preservation
all pass. Agite was never used for test removals or script edits, and the app is back on its original local workspace.

All original IDs/counts remain: one user, 59 leads, two numbers, four calls and eleven tasks. Memberships, list catalog,
pause/removal flags and Agite master v3 hash match the fresh baseline; both owner-removed contacts remain removed.
All 55 requested active leads have current scripts, with no refresh pending. Master-v3 usage is unchanged at 1,528,794
known tokens across 32 requests, four historical unknowns and zero running. Both numbers show ready, pending setup
is zero and capacity refresh is current at 22:04 Pacific. Providers, greeting and 08:00–17:00 hours remain; dialing
is paused with all active lists. Temporary PostgreSQL/native services are stopped, test data and extracted apps are
in Trash, and the awake helper is stopped. No new carrier/microphone session or real generation was opened.
The wider number/connect-rate plan remains open for its separate live and measurement gates.

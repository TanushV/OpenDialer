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
- [ ] Pass affected and full checks; bump/commit/publish the signed 1.0.1 patch.
- [ ] Update the authorized Mac, verify through computer use with isolated examples, retain production setup/data.
- [ ] Commit final evidence/handoff and close only after all items pass. Keep wider carrier/measurement gates open.

## Checkpoint

Started from clean main `14f4972`, installed 1.0.0. Native inspection and read-only audit show removal attempts
reported success while the blank source meant Unnamed list; two lead archives succeeded but remained in the
unfiltered table. No production records have been changed by this follow-up. The implementation will reuse
archive state and list memberships, with no new deletion mechanism or database schema.

Focused verification: 31 acceptance cases pass on each store. The browser regression exposed a real PostgreSQL
lead-version conflict when duplicate import overlapped preparation; duplicate membership edits now reload and lock
the contact before updating it. A further failing check found newly imported contacts were absent from the new
filtered history view; import now reloads that view. Removed list names no longer reappear in shared-contact
preparation. Seven focused browser cases passed before the final import-display regression; final/full checks follow.

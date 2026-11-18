# CSV intake, script paths and calling context

Approved November 17, 2026. Read this plan and the Status section of [the rebuild plan](REBUILD_PLAN.md)
before continuing and after every context compaction. Update the checkpoint below as work lands.
Build directly on main, test before each fix, and run the fast and full checks under section 12A.

## 1. Import CSVs

- Accept commonly exported phone, name, company, domain, and location headers regardless of case and spacing.
  Support full names or first and last names, optional blanks and omitted trailing optional cells. Keep original
  columns for the agents. If multiple phone columns are plausible, show the current choice with an override.
  Reject duplicate headers and ambiguous/malformed values with usable row numbers. Keep valid rows importable.
- Show the canonical name, phone, company and phone status in the import preview. Add Copy recommended CSV headers
  beside Open lead list. The header row uses name, phone, email, company, domain, title, linkedin, headline,
  company description, industry, company size, state, timezone, number source, phone status, account signals,
  signal dates and signal links. Explain the optional fields and existing signal separators.
- Keep every imported cell under the existing file/row-count limits and use one row in the research and writing
  stages. Bound the prompt view for wide rows, with an agent tool to fetch an individual full cell. Review resolves
  only the source cells cited for each claim. Treat cell contents as data. Import does not start generation.
- An optional `script` column carries the lead's finished text. Quoted multiline Markdown and plain text both work.
  Import saves a complete script as ready for that contact. Blank cells follow ordinary preparation. A row with an
  unfinished or malformed script names the problem in the import receipt.
- Import large CSVs into one list through bounded requests. Combine the counts in one receipt and show the original
  CSV line for each skipped row, including scripts with multiline cells.

## 2. Remove and reimport

- Remove list frees the local name, drops the membership and settings, and discards list-only preparation state
  and pending work. An older Removed list can be permanently cleared. Other list memberships still work and their
  preparation is not invalidated. Keep opt-outs, active obligations, callback identities, and saved call receipts.
  A call or reservation in progress must not be stranded by removal. A reimport gets fresh list state.
- Existing records with no active remaining membership may be reused as historical contact identities. A fresh
  CSV list does not inherit their archive/pause flags, scripts or per-list cadence. If saved call receipts keep the
  old identity, keep those receipts available for callbacks and exports. Do not erase or unpause another active
  list's records through a reimport. Import receipts distinguish newly created contacts, existing matches, and skips.
- Check CSV call history read-only in HubSpot when the rep starts calling, for the selected lists only. Reuse the
  existing credentials and unambiguous contact matching. Count logged outbound attempts (including no answer)
  for the list's cadence, show the latest attempt, and report ambiguous/unavailable results explicitly. CSV import
  and removal do not sync CRM records. Existing post-call linking/writeback stays. HubSpot history cannot recover
  old unlogged attempts on unlinked CSV contacts. Prove the check with recorded/simulated CRM responses.

## 3. Prepare and show one path

- Optional authored Markdown supports named paths, path selection guidance and a default. Mark options with path
  annotations. Choose exactly one path after research, then validate that every selected option belongs to that
  path or is shared. Render in authored order, including unchanged common objections and any shared responses.
  Preserve existing Markdown behavior for masters without path declarations. Document and preview the syntax.
- Keep the exact rendered generated script viewable in Leads, including the last generated version marked Stale
  after inputs change. Ready versions drive prepared lists and are used at reservation and dial. Preserve the
  selected path in call snapshots and export it with the existing script version/structure. Verify the tracker
  and helper use the same rendered path. Do not inject the context card into spoken text.
- Put a brief above the existing context on the left of the Dialer. Use name, role, company name, what the company
  does, and the saved signal. Prefer prepared evidence and imported fields with accurate source labeling and sensible
  behavior for missing evidence. Reuse existing lead/packet data. No model request during the call to fill it.

## 4. Verify and deliver

- Add regression tests before implementation. Cover unusual CSV headers, phone ambiguity, wide rows and cited
  cells, removal/reimport with older and shared records, cancellation/races, and HubSpot match/call/error cases.
- Use Founder and Sales Leader fixtures with the same authored master. Prove each lead has one rendered path,
  unchanged objections, exact author order, refreshed/stale preview, and matching simulated call and export.
  Verify the left-hand brief on the real app layout in WebKit with sparse and populated fixtures.
- Run fast and full checks, commit and push to main, and verify exact commit CI. Report simulated proof separately
  from installed or live proof. Publishing and installing follow the authorization in the rebuild plan.

## Checkpoint

- Baseline main `0c91171` passed Verify run 44066336901.
- CSV alias intake, header copy/preview, row-level failures, and full-cell agent access are implemented.
  Focused backend intake and why-now tests passed. The left Calling context now reads the imported role,
  company description and signal or the saved research summary.
- Permanent local list removal and same-name reimport passed focused tests. Existing Removed lists can be deleted
  by the operator. Shared contacts, suppressed phones and saved calls stay associated with their original identities.
- Path syntax is `# Paths`, then `- path-name: when to use it` and an optional `(default)` after the path name.
  Mark named options with `[path: path-one, path-two]`. The backend passed a two-role preparation and stale
  preview test. A new WebKit journey verifies both perspectives and the Dialer brief.
- HubSpot history runs just before offering each selected CSV candidate, with a bounded read and a short cache.
  Known calls apply existing cadence and attempt caps. Missing matches are eligible, and unavailable or ambiguous
  lookups report their state while the current local calling rules still apply. The mocked CRM suite passed.
- The focused WebKit batch passed all 11 journeys, including relaxed headers, named-list reimport, both prepared
  paths, and the left-hand context panel in a simulated call. A copy of the owner's saved master was tagged
  in a private temporary file and parsed with its original section order and all nine objections on every path.
  The live workspace master was left as saved.
- Source and focused tests are complete. Before a release, verify the full pre-push suite and exact-commit
  GitHub CI from section 12A. The installed app and hosted settings were untouched.
- The first full local suite passed. CI Chromium exposed a race in the existing ending-call fixture: an
  intercepted refresh could show Ending before the test clicked Hang up. The test now starts the simulated
  confirmation delay after the hang-up POST. Six focused Chromium replays passed.
- CI WebKit confirmed that a same-name reimport returned zero new and one matched contact, as intended.
  The test now awaits that exact import receipt instead of reading the previous receipt from the page.
- November 17: main `ba43dca` passed all eight Verify jobs. The owner approved publishing 2.2.4 and updating
  this Mac. Verify the signed artifacts and installed version, and use the installed backend's API in an
  isolated workspace to import the 306-row Downloads CSV, prepare a sample, and verify list deletion.

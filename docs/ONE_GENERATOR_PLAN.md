# One script generator: plan

Proposed and approved November 23, 2026. **Status: Steps 0, 1, 3 and 4 done; Step 2 (release) next.** Read this
alongside [the rebuild plan](REBUILD_PLAN.md) and its Status section. Build rules are section 12A of that plan: one
worker, on `main`, tests first, fast check after each change, full check before each push.

## TLDR

- Today there are two script generators: the app's own preparation, and the outside CSV generator in
  `~/Documents/OpenDialer Script Generator`. They share the app's checks but run differently, and every bug found on
  November 23 was in the outside copy.
- Make the app's preparation pipeline the only generator. Add a command-line run of it on a lead CSV and an export of
  the finished scripts with their research. The outside folder becomes a launcher for that command.
- Imported scripts then carry their sources, so the Dialer can show where an observation came from and why now.
- Small fixes now (Step 0), then about 2 to 3 working days of app work, one signed release, and a side-by-side check.

## 1. Why

The two generators differ in how they run, not only in code:

| | In-app preparation | Outside CSV generator |
|---|---|---|
| How the AI works | A session: it sees each error and fixes it in place | One shot per lead through `codex exec`, retried fresh with only the error text |
| Facts that fail the check | Kept out; the passing ones stay saved | Until November 23, any failing fact failed the whole lead |
| Output | A stored script with its facts and trigger | Markdown in a CSV cell, plus path and version columns |
| Research after import | Shown in the Dialer | Lost: the Dialer says "No trigger found" |

Sharing more code would narrow the gap, but two runners will keep drifting apart. One pipeline removes the gap.

## 2. What happened on November 23

- `~/Downloads/cold-call-script-v14.1.txt` is v14 in the app's format: 10 paths, 7 AI blocks, 49.5 KB. Each lead's
  finished script keeps one path and is 29 to 31.5 KB.
- The app's master limit is 64 KB on local `main` (`2787eee`, not pushed). Installed 2.4.3 still refuses masters over
  40 KB, so it cannot save v14.1. It imports the finished scripts, which are under 40 KB.
- Outside generator fixes made in its folder (no version history there): notes-only sections now stay notes;
  `# Before you dial` and `# Triggers` are left out of finished scripts; facts are checked one at a time and
  citations are remapped (the app numbers facts in the order it accepts them); 3 tries instead of 2; the prompt
  steers away from sites that block automated reads. Its 6 local tests pass.
- Agite run (237 leads, `--workers 4`): first pass 199 ready, 38 failed (23 pages would not load, 13 quotes did not
  match, 4 bad trigger dates). The retry with the fixes made 33 more. 232 are in
  `~/Downloads/Agite_Customer_Trestle_DNC_Cleared_2026-11-23-scripts-ready.csv`; every row passes the 2.4.3 import
  check.
- 5 leads had no script. Each company's own site refuses the app's page reader, and the AI cited it on all 3 tries.
  Step 0.1 later fixed all 5 (see Checkpoint).
- Paths chosen: from-where-you-sit 88, runs-it 61, does-it 28, technology 20, operations 12, senior-runs-it 12,
  marketing 9, finance 2. No founder got founder-sells or role-check.

## 3. Decisions

| # | Decision | Status |
|---|---|---|
| G1 | The app's preparation pipeline is the only script generator. The outside folder keeps only a launcher and its README. This narrows the November 22 decision that the generator stays outside the repo: the launcher stays outside; the logic moves in. | Owner approved, November 23 |
| G2 | Completed CSVs keep `script_format` 1. Research travels in new optional columns. Older apps import those columns as ordinary lead data (only the six exact script header names count as a script column), so 2.4.3 still imports the files. | Recommended |
| G3 | The command-line run uses the same AI route as the app, signed in with the author's Codex login, on a temporary workspace that is not the installed one. | Recommended; depends on R1 |
| G4 | Today's `generate_scripts.py` stays as the fallback until Step 4 shows the new path matches it, then it is retired. | Recommended |

## 4. Steps

### Step 0: now, no release

1. Retry the 5 missing leads with the current generator. The retry prompt names the pages that could not be read and
   says to use other pages or the CSV row instead. Done when all 237 have scripts or each remaining one has a stated
   reason.
2. Loosen the founder guidance in v14.1 so a founder with evidence of selling gets founder-sells, and a founder
   at a company that may have no sales team gets role-check. Done when a 10-founder sample picks those paths where
   the evidence supports it.

### Step 1: one generator in the app (repo, tests first)

1. **Command-line run.** `app.cli generate-scripts LEADS.csv --master FILE.md --out FILE.csv` imports the leads into a
   temporary workspace, runs the app's normal preparation (same route, checks, retries and concurrency settings) and
   exports. It resumes from that workspace after an interruption and never touches the installed workspace.
2. **Export.** Each ready script is written as Markdown with `{rep}` and `{seller}` left as placeholders, plus
   `script_path`, `script_master_digest`, `script_format` and `script_generator`. New research columns hold the facts
   (statement, quote, source URL), the trigger (type, date, source, strength) and which facts came from the web and
   which only from the CSV row. Research columns stay within the import's cell limit.
3. **Import.** When research columns are present, the importer stores them with the script, and the Dialer shows the
   observation's source and the why-now signal. Imported research is labeled as from the author's run, not as new
   verification. Files without those columns import as today.
4. **Page reader.** Find out why some company sites are refused as unsafe and why others return errors. Fix what is
   safe to fix, and tell the agent which pages it could not read. This helps in-app and CSV runs alike.
5. **Size limit.** The 64 KB master limit is already committed (`2787eee`).

Tests, written before the code they cover:

- Export then import gives the same script text, path and research.
- A script generated in the app matches its exported and re-imported copy.
- A file with research columns imports in 2.4.3-compatible form (format 1; extra columns as row data).
- Every v14.1 path renders, stays under 40 KB as a finished script, and keeps rep notes as notes.
- A dropped fact never shifts another fact's citation.
- A page the reader cannot load leads to a retry that avoids it.

### Step 2: release (needs the owner's OK)

Full check, push, green CI on the exact commit, then a signed release. This release is needed: it lets the installed
app save v14.1 as its master and show research on imported scripts. Update this Mac only on the owner's request.

### Step 3: the outside folder

Replace its logic with a launcher that calls the repo command, and rewrite its README. Keep `generate_scripts.py` as
the fallback until Step 4 passes (G4).

### Step 4: prove it

1. Run the 4 sample leads and 20 Agite leads through the new path. Compare with the November 23 scripts: path,
   observation, research. Differences must be explained, not just smaller.
2. Import the result into a throwaway app, then into the installed app with the owner's OK.
3. Generate a few leads inside the app with v14.1, which has never been run.
4. Record the results in the Status section of the rebuild plan. Live calls stay a separate gate.

## 5. Risks

| # | Risk | Answer |
|---|---|---|
| R1 | The app's AI route may not run without the app window, using the author's Codex login. | Check first in Step 1. If it cannot, fall back to two runners that share all acceptance, rendering and export code; that still fixes every bug found on November 23. |
| R2 | In-app generation on v14.1 is untested. | Step 4.3, before retiring the fallback. |
| R3 | Research columns make CSVs larger. | Bounded columns, covered by the large-import test. |
| R4 | The AI copies fixed wording into its lines, and the checks verify facts, not wording. | Skim a few rows of every batch; v14.1's block instructions name the exact wording to copy. |

## 6. Out of scope

Live calling changes, HubSpot sync, pricing or usage limits of the author's Codex plan, and bundling the launcher in
installers.

## Checkpoint

- November 23, 2026: plan written and approved.
- Step 0 done. The old generator named blocked sites in its retries and finished all 237 Agite leads
  (`...-scripts.csv`, every row under 40 KB). v14.1's founder guidance now prefers role-check for early-stage founders.
- Step 1 done. `python -m app.cli generate-scripts` (`app/preparation/batch.py`) runs the app's own preparation on a
  CSV in a private work folder through its HTTP API, waits for retries, prints progress and writes the output. It
  copies the Codex CLI login in and writes a rotated login back (checked: still signed in after a run).
  `GET /v1/preparation/export` (`app/preparation/portable.py`) exports ready scripts with `{rep}`/`{seller}` and a
  `script_research` column; import stores that research, and Leads and the Dialer show "checked when the script was
  written" with the quote and source. The page reader follows 5 redirects, sends Accept headers and tells the agent
  why a page failed (for example HTTP 403). Tests: `test_portable_scripts.py`, `test_generate_scripts_cli.py`,
  `test_research_fetch.py`, `test_prep_threads.py`.
- R2 found in Step 4: the in-app review dropped every AI line in v14.1, because each block also stated the seller's
  own claim or copied a question. v14.2 (owner's Downloads) keeps only the factual observation AI-written and makes the
  reason line and questions fixed text. Dropped lines are now saved and listed by the export and the run.
- Step 3 done. `generate-scripts` in the outside folder calls the repo command; `generate-scripts-legacy` keeps the old
  one until retired (G4).
- Step 4: 24 real leads (4 samples, 10 founders, the 5 formerly blocked) on v14.2: 24 of 24 ready on the first pass,
  about 18 keep their AI observation and the rest open with the fixed reason line and question; the review's reasons
  for the rest are not yet stored. Imported into a throwaway app: 24 ready, why-now and source shown in the Dialer.

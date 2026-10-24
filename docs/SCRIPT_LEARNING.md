# Script learning (implemented October 24, 2026)

Agreed with the owner on October 24, 2026. OpenDialer learns how prospects react to each part of a campaign's base
script, keeps that knowledge in one continually edited Markdown memory per campaign, and uses it to improve the
script through controlled tests. A campaign is one product or service and can run for months.

## Agreed rules

- The seller supplies the base script. Learning never rewrites it from correlations: the memory proposes a change,
  the change runs as a controlled variant against the base (stable 50/50 assignment with logged odds), and it
  replaces the base only when it wins with enough matured leads behind it.
- Promotion is automatic by default; a campaign setting can require an administrator's approval instead. Every
  adoption is recorded and can be rolled back.
- Identity and compliance stay locked: who is calling (`{rep}` and `{seller}` in the opener), the screening line that
  names the seller, and the fact-backed proof slot. No guarantees, invented relationships or claims the research does
  not support.
- Expect "not enough calls yet": a pattern needs at least 10 separate leads on each side before it is described, and
  a later period must confirm it before it counts as supported. Everything is labeled as correlation except
  controlled test results.
- Talk ratio and other call-shape measures are post-call only: memory, reports and call review, never live nudges.
- Raw speech still expires within five minutes. Only labels, counts and timings persist; the memory holds seller-
  authored script text and numbers, never prospect words.

## Data

| Where | What |
|---|---|
| Transcript segment metadata | `word_count` for every final segment; `script_section` and `section_score` for rep segments that match a section of the call's script (computed while the text exists) |
| Call `script_snapshot` | the base script `script_version`, the test `script_arm` and `proposal_id`, and a short `variant` label per section |
| Call `script_trace` | written at finalization from metadata and reviewed observations: delivered sections in order with start, portion length and the prospect's reaction (`engaged`, `objection`, `brief`, `silent`, `ended`), the opening, and call shape (talk ratio, longest monologue, questions, time to first question, duration) |
| Campaign `script` | the campaign's base script (falls back to the seller template), version history, and settings (`auto_propose`, `promotion`) |
| Campaign `script_proposals` | proposed section changes and their status: suggested, testing, won, promoted, lost, inconclusive, rejected |
| Campaign `controlled_comparisons` | script tests use family `script` (control = base version, candidate = proposal) |
| `script_memories` table | one row per memory version: the Markdown document, the team notes, who or what wrote it |

## Statistics

Each call's trace becomes categorical features: opener variant, reaction to each section, opening length,
discovery questions asked, time to first question, rep talk ratio and longest monologue buckets. They go through the
same estimator as conversation patterns: one first eligible conversation per lead or shared endpoint, label embargo,
comparison within matched mix strata, multiple-comparison-corrected intervals, and confirmation in a later period.
Section reaction summaries (delivery count, reaction mix, close rate after each reaction) are descriptive.
"Close" is the campaign's conversion goal (meeting held by default).

## The memory document

Rewritten after each learning refresh between `<!-- learned:start -->` and `<!-- learned:end -->`: what is working,
what is not working, too early to tell, call shape (post-call), script tests and their progress toward the
required leads, and script versions. Everything outside the markers is team notes, which the system never
overwrites. Admins edit team notes with a version check; every version is kept; the memory downloads as `.md`.

## Uses

- **Script writing** (per-lead slot filling) gets a bounded digest of the memory and the team notes.
- **Proposals**: with `auto_propose`, after a refresh with no test running, the script writer proposes one change
  for the weakest section (lowest engagement among sections delivered to at least 10 leads). Proposals pass the
  locks and the template validation, then start a test automatically. Admins can also propose a change by hand.
- **Live help** gets the supported section findings for the script in use, never call-shape measures.
- **Promotion**: after each optimization refresh, a script test that has sufficient evidence and a better candidate
  is adopted (or waits for approval); a candidate whose interval is clearly worse stops early; a test that ends
  without enough evidence is inconclusive. The old opener-only "treatment variants" are replaced by this mechanism.

## Tests first

New HTTP journeys in `services/backend/tests/acceptance/test_feature_script_learning.py` and unit tests for section
matching, reactions and feature buckets; browser coverage for the memory panel and proposal flow. All suites stay
green on SQLite and PostgreSQL.

## Status: implemented

| Piece | Where |
|---|---|
| Section matching, reactions, call shape, features, report, Markdown, locks | `services/backend/app/optimization/script_learning.py` |
| Base script versions, memory versions, proposals, tests, settlement, auto-proposal, routes | `services/backend/app/optimization/script_lab.py` |
| Shared association estimator (conversation patterns and script features) | `services/backend/app/optimization/conversations.py` (`first_eligible`, `association_patterns`, `supported`) |
| Segment labels at arrival | `privacy.py` (`script_section_of`, persisted `word_count`, `script_section`, `section_score`) |
| Trace at finalization; trace in call review | `intelligence.py` (`_materialize_views_in`, `analysis`) |
| Campaign base script and test arm when rendering | `preparation/workflows.py` (`script_base`, `selected_script`), `scheduling/queue.py`, `service.py` |
| Learning refresh, test settlement | `optimization/workflows.py` (`refresh_learning`, `refresh_optimization`) |
| Script writing and live help inputs | `intelligence.py` (`script_memory` in slot filling), `coaching/workflows.py` (`script_learning`), `providers/openrouter.py` |
| UI | `apps/desktop/src/components/ScriptMemory.tsx` (Intelligence page), script trace on call review (`ReviewPage.tsx`) |
| Tests | `tests/acceptance/test_feature_script_learning.py` (9 journeys), `tests/test_script_learning.py` (15), `apps/desktop/tests/script-memory.spec.ts` |

Routes: `GET /v1/optimization/script`, `GET /v1/optimization/script/memory.md`, `PUT /v1/admin/script/memory`,
`PUT /v1/admin/script/settings`, `POST /v1/admin/script/proposals`, `POST /v1/admin/script/proposals/{id}/start`,
`.../approve`, `.../reject`, `POST /v1/admin/script/rollback`. The old `/v1/admin/variants` route and the
`treatment_variants` table are gone.

Notes: memory versions are created when findings, test states, script versions or team notes change; between
those, the latest machine version is refreshed in place with current counts. Script tests take a minimum
detectable effect (0.05-0.5) that sets the planned leads per arm; the decision still requires the difference
interval to clear zero and no rep stratum where the candidate loses. Live gates are unchanged: real reactions need
Telnyx transcription with timestamps and reviewed interpretations from a funded model.


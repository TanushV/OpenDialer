# Markdown scripts and token usage

Use **Script** to paste or upload one Markdown playbook. The preview lists structural errors with line numbers;
save creates a version you can inspect or roll back. Parsing the Markdown uses no AI tokens.

`# Opening` (or `# Opener`) and `# Close` (or `# Book the meeting`) are required. Every opening names the caller
with `{rep}` and `{seller}`. Available fields also include `{name}`, `{first_name}` and `{company}`.
Screening and Discovery are optional. Without Screening, the app supplies a business introduction from the rep
and workspace names. The rep handles a screening prompt on the connected line.

`# Pitch` accepts the alias `# Problem and outcome`. Use `##` headings for named alternatives within any spoken
section. The agent picks one opening, pitch and close, up to three discovery questions and two offers. The Dialer
keeps the heading and option order from the file and folds alternate options in place. All objections stay available.
Existing bullet alternatives remain accepted. Custom headings with speaking text can be tracked like any other
section. Put internal directions in blockquotes or a reference heading such as `# Instructions`.
`# Goal`, `# Instructions`/`# Delivery rules` and
`# Preparation`/`# Before you dial` provide non-spoken context. A blockquote (`>`) is a rep direction, shown separately
from spoken wording. For conditional branches, put the question in the wording and describe when to use the next
line in a direction. The script remains a guide for a human conversation, not an automatic speech engine.

Write `{{ one short instruction grounded in verified facts }}` where the agent should produce fresh per-lead
wording. The generation gets the imported lead row, verified unexpired research, the current master, team memory
and bounded prior-call summaries. It does not receive the old personalized script as a draft to revise.
Unsupported facts, numbers, guarantees and relationships are refused by the existing validators/reviewer.

Keep each `{{ }}` block to facts about the lead, such as an observation drawn from their role or a sourced signal.
Write your own claims ("We work with sales teams...") and your questions as fixed text beside the block. The
independent review keeps AI text only when every assertion in it follows from the lead's sources, so a block that
also states what your business does, or copies a question with assumptions in it, is dropped, and that option loses
its AI line. The script is still published; the export and the command-line run below list any such drops.

## Script tests (variants)

Declare one test in a `# Variants` section: a bullet per variant with its share and what you expect it to do, and
optionally `When:` to count only leads with certain triggers.

```
# Variants
- direct-open (50%): Hypothesis: naming the reason first gets past the opener more often.
- permission-open (50%): Hypothesis: asking for thirty seconds cuts early hang-ups.
When: hiring_sellers

# Opening
## Direct [variant: direct-open]
Hi {first_name}, {rep} with {seller}. I'm calling because ...
## Permission [variant: permission-open]
Hi {first_name}, {rep} with {seller}. Can I have thirty seconds to tell you why I called?
```

Shares add up to 100%. Tag options with `[variant: name]` (beside `[path: ...]` if you use paths); untagged options
are shared by every variant, and every variant needs an Opening and a Close. Code, not the AI, gives each company one
variant from a stable draw, so the comparison is fair, and the script for that lead keeps only its variant's options.
The variant rides with the script, the call, the completed CSV (`script_variant`, `script_variant_test`) and the
statistics export. Review shows each variant's hypothesis and results, and says "not enough data" until a variant has
30 conversations. Changing the shares, the names or `When:` starts a new test.

## Finished scripts for other computers

`GET /v1/preparation/export?list_name=...` returns a list's ready scripts as portable Markdown, with `{rep}` and
`{seller}` left for the receiving rep, plus `script_path`, `script_master_digest`, `script_generator`, the script test
variant (`script_variant`, `script_variant_test`) and
`script_research` (the facts behind the script, their quotes and links, and the why-now signal). Importing that CSV
in Leads marks each lead ready and shows the why-now as "checked when the script was written". Apps before 2.4.4
import the same file and keep `script_research` as ordinary lead data.

To run the whole thing on a CSV without opening the app:
`python -m app.cli generate-scripts LEADS.csv --master MASTER.md`. It imports the leads into a private workspace beside
the output, runs this same preparation with the signed-in Codex login (`~/.codex`), waits for retries, and writes
`LEADS-scripts.csv`. Running the same command again continues the same work. See
[the one-generator plan](ONE_GENERATOR_PLAN.md).

Meaningful master edits make earlier personalized scripts stale and queue fresh generation for leads already
requested for preparation. While that runs, the app displays the current base, never an old personalized draft.
Formatting-only edits keep the generation identity and do not consume model work. Active calls retain the script
frozen when they started. A job for superseded input cannot publish its script as current.

During the call, the script panel follows confidently matched rep phrases. It holds during prospect speech and
tangents, and follows the next match even after you scroll manually. Live helper uses the transcript or an F1 or
typed private request to pick a response from that
frozen script. It displays the prepared wording as written. Put spoken alternatives in the named spoken sections,
such as Objections. The rep can read custom reference sections and the This call notes in the script.

After saving, the editor tells you whether the edit needs a rewrite. **Prepared scripts** on the Script page shows
the current master version, unique active lead count and ready/waiting/in-progress/failed results. If AI is disconnected
or preparation is paused, it shows the action needed. Paused or removed contacts refresh after they become active again.

Use **Token usage and fresh scripts** to choose a list and one of two research policies: reuse current research
or refresh research and rewrite. Run a three-lead sample, inspect actual receipts, then **Rewrite remaining leads**
or explicitly rewrite the whole active scope. Paused/archived contacts and lists, and opted-out endpoints, are
excluded from these explicit rewrite actions. Estimates describe eligible remaining work for the current master;
they use up to 100 recent reported writing/research and review receipts. Searches, retries and review can change
actual consumption, so these are estimates rather than quotes.

The usage panel filters by campaign/list, master version, work type and optional lead ID. It shows reported input,
output, cached and reasoning counters, model/route, master/job and request outcome. Cached tokens are a subset of
input and reasoning is a subset of output: do not add those subsets to total tokens. Research and writing share
one agent request and are labeled together; independent review has its own receipts. Cumulative subscription
updates are deduplicated per request, including consumption reported before failure or cancellation. A shared
thread has an exact reported total and an explicitly estimated equal allocation per lead.

ChatGPT subscription work is labeled subscription-included, not a metered dollar charge. Provider charges and
unpriced receipts remain distinct. Missing counters display **Unknown**; historical preparation usage before
0.10.0 was not recorded and cannot be recovered from the app ledger. Reported totals exclude missing counters.

Offline maximum-effort script review can take several minutes; 0.10.1 gives it up to fifteen minutes.
From 0.10.2, independent requests lock the personal login only while copying or saving credentials, so one
long review does not block another review or preparation thread. This changes no live calling or live-helper
deadline. Reported usage from interrupted attempts stays in the ledger.

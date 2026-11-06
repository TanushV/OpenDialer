# Markdown scripts and token usage

Use **Script** to paste or upload one Markdown playbook. The preview lists structural errors with line numbers;
save creates a version you can inspect or roll back. Parsing the Markdown uses no AI tokens.

`# Opening` (or `# Opener`) and `# Close` (or `# Book the meeting`) are required. Every opening names the caller
with `{rep}` and `{seller}`. Available fields also include `{name}`, `{first_name}` and `{company}`.
Screening and Discovery are optional. Without Screening, the app supplies a business introduction from the rep
and workspace names. The rep handles a screening prompt on the connected line.

`# Pitch` accepts the alias `# Problem and outcome`. Use `##` headings for named alternatives within any spoken
section. The agent picks one opening, pitch and close, up to three discovery questions and two offers; all
objections stay available. Existing bullet alternatives remain accepted. Custom reference sections are preserved;
ordinary bullets there stay checklists. `# Goal`, `# Instructions`/`# Delivery rules` and
`# Preparation`/`# Before you dial` provide non-spoken context. A blockquote (`>`) is a rep direction, shown separately
from spoken wording. For conditional branches, put the question in the wording and describe when to use the next
line in a direction. The script remains a guide for a human conversation, not an automatic speech engine.

Write `{{ one short instruction grounded in verified facts }}` where the agent should produce fresh per-lead
wording. The generation gets the imported lead row, verified unexpired research, the current master, team memory
and bounded prior-call summaries. It does not receive the old personalized script as a draft to revise.
Unsupported facts, numbers, guarantees and relationships are refused by the existing validators/reviewer.

Meaningful master edits make earlier personalized scripts stale and queue fresh generation for leads already
requested for preparation. While that runs, the app displays the current base, never an old personalized draft.
Formatting-only edits keep the generation identity and do not consume model work. Active calls retain the script
frozen when they started. A job for superseded input cannot publish its script as current.

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

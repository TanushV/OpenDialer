# Approved language and evaluation

Publish a truthful business introduction and screening line in Intelligence. Cards contain short approved questions/responses and tags. Personalized factual statements require current first-party evidence; unsupported SEO metrics, guarantees and invented previous relationships are rejected. Publishing a version retains the old script and a diff. Active call packets do not change mid-sentence.

Use keyword trackers for literal phrases and semantic trackers for a defined concept with positive/negative examples and transcript citations. Preview changes on a frozen reviewed set. A semantic hit is rejected if its evidence does not exist. Human notes remain separate from AI revisions; a conflicting human edit requires review.

The scorecard uses observable, evidence-linked items with N/A and INSUFFICIENT_EVIDENCE and shows its denominator/coverage. A short wrong-number call cannot become a poor discovery score. Team associations are correlations. Reviewers can challenge scores and curate evidence-backed snippets before publishing recommended wording.

`packages/evaluation-fixtures/coach.jsonl` is explicitly synthetic. Run `GROQ_API_KEY` from a protected environment with `uv run python scripts/evaluate_coach.py --output work/coach-benchmark.json` to compare 20B and 120B. This measures inference/grounding only. Add permissioned real replays and balanced human usefulness reviews before asserting end-to-end latency or outcome improvements.

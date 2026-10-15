# Working agreement

Read `docs/spec/IMPLEMENTATION_SPEC.md`, `docs/PLAN.md`, and `docs/STATUS.md` on every continuation or context compaction. The user's standalone local mode is an explicit extension of the original shared-server specification. Do not lose either mode.

Write acceptance tests before implementation and retain the red/green evidence in `docs/STATUS.md`. Add regression tests before fixing defects. Update the plan checkboxes as work passes its checks. Commit coherent milestones frequently. Never commit credentials, app databases, recordings, build products, or test caches.

Tests with simulated providers are evidence about software behavior only. Carrier calls, hardware audio, production hosting, account eligibility, legal approval, and outcome lift require their own evidence. Keep those gates explicit; never convert an absent API key into a successful integration claim.

Preserve installed application identity and user data. Use the signed updater for released builds. During local development replace the existing bundle atomically; do not uninstall/reinstall or reset data.

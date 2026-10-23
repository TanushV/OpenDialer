OpenDialer 0.4.0: the lean build, for internal use.

Every 0.3.3 feature stays: calling with ready/pause, next lead, bridged Telnyx legs, keypad, opt-out and final-attempt voicemail; inbound return calls; compliance screening; the Cerebras live helper; post-call rundown, scorecards, rubrics, tasks and notes; coaching; preparation and research; statistical estimates, model training and trials, experiments and conversation learning; numbers, capacity and health; metrics, work time, costs; settings, users, backup and restore.

What changed underneath:
- Plain storage: no encryption at rest, journals or migration chain; reports computed straight from the records.
- Faster on a store with 10,000 leads and 100,000 calls: next lead, wrap-up and the workspace view are 2.5 to 4 times quicker, and startup takes under 0.1 s.
- A smaller desktop shell: it only launches the local service and the webhook forward.
- Provider keys are entered in Settings and stored unencrypted on the authority.

0.4.0 starts with a fresh workspace and does not read 0.2/0.3 data. On a machine that ran 0.2/0.3, the old workspace stays untouched in the app data folder (`local/` and `simulation/`); 0.4.0 keeps its own in `local-2/` and `simulation-2/`. A server pointed at a 0.2/0.3 database refuses to start and says why.

Simulated providers prove software behavior only. Live Telnyx calls, funded Cerebras quality and latency, DNC vendor access, provider retention terms, hardware audio, connected-server latency on a dedicated host and campaign lift remain to be tested live. Packages carry pinned-key updater signatures; trusted Apple/Windows publisher signing and notarization are not configured.

---

Historical 0.3.3 release notes follow.

OpenDialer 0.3.3 — developer preview; installed-update validation in progress.

0.3.3 replaces 0.3.0, 0.3.1 and 0.3.2, each withdrawn on October 21 after installed-update validation found a product defect. Installed data stayed intact throughout.
- 0.3.0, on Windows, could not open a workspace it had just migrated from 0.2.1: the upgrade flushed its retained recovery copy through a read-only file handle, which Windows rejects (macOS accepts it). The upgrade, lock and journal suites now also run on Windows in CI.
- 0.3.1 opened and used the migrated workspace but could not sign out of it: 0.2.1 left simulated prospect legs open behind ended calls, and 0.3.x counted them as live. Startup now closes a terminal call's leftover prospect leg, as 0.3.x does for its own calls.
- 0.3.2 passed the MSI update end to end, but once kept raw speech on screen past five minutes: the desktop's expiry timer could fire while its wall and monotonic clocks both still read "live", and nothing rescheduled it. The expiry watch now checks again until the speech is gone.

Nothing else changed from 0.3.0.

0.3.3 is the lean completion of the calling workflow:
- A proactive Cerebras helper (through OpenRouter, zero data retention) sits above the prepared script.
- One conversation state feeds live help, the post-call rundown and the next call.
- Local callbacks and tasks, plus rep-reported meetings.
- Preparation runs in advance, with gatekeeper and iPhone screening sections.
- Sparse statistical estimates, one capacity view from your existing numbers, and observed number health.
- National and internal do-not-call screening before every US submission.
- Final-attempt-only voicemail.

New subscriptions or number rentals, call recording and CRM/calendar sync stay disabled. Raw speech and quote-bearing helper content expire within five minutes. Research and script writing can optionally use your own ChatGPT plan; the live helper never does.

The authority is smaller and faster:
- An idle authority makes no database writes.
- Calling work runs ahead of background work.
- Next-lead selection, the workspace view and reports stay bounded as history grows. At 100,000 leads and 1,000,000 attempts a warm next-lead decision takes about 21 ms.
- Training runs in a separate bundled trainer, and models are served from JSON.

Concurrency, storage and privacy defects found by whole-system load, scaling and native runs are fixed. The validation record lists them with their tests.

Existing users must update in place through Settings → App updates; never uninstall or reset. The app identity and workspace data are preserved. Mac ARM64 (macOS 14+) and Windows x64 packages include their backend and trainer, and simulation works without provider keys.

This release is published so the existing 0.2.1 updaters can be exercised. It becomes an accepted update only after the Mac and Windows in-place updates pass; the validation record will say so. Live carrier calls, funded Cerebras quality and latency, DNC vendor access, provider retention terms, hardware audio and measured campaign lift remain separate gates. Packages carry pinned-key updater signatures; trusted Apple/Windows publisher signing and notarization are not configured. Connected (server) latency is not yet certified on the 2-vCPU reference.

---

Historical 0.2.1 release notes follow.

OpenDialer 0.2.1 adds measured number demand/health, immutable operational funnel learning, future scheduling, reviewed model pilots, conversion corrections, controlled number/coaching comparisons and grounded live guidance through Cerebras via OpenRouter. Both isolated local SQLite and authenticated shared PostgreSQL modes use the same authority services.

Existing users update through Settings → App updates. The stable application identity and workspace are preserved; do not uninstall or reset data. Mac ARM64 (macOS 14+) and Windows x64 packages include their backend. Simulation works without provider keys. Live calling needs Telnyx configuration, owned callback-capable numbers and reviewed campaign permissions; live inference needs a configured funded account.

Provider/media/hardware certification, representative model-quality review and measured campaign lift remain separate acceptance gates. A tiny or synthetic trial cannot prove higher pickup/conversion rates. Preview packages have pinned-key updater integrity signatures; trusted Apple/Windows publisher signing and notarization remain external gates.

See [optimization and evaluation](https://github.com/TanushV/OpenDialer/blob/main/docs/OPTIMIZATION.md), [validation](https://github.com/TanushV/OpenDialer/blob/main/docs/VALIDATION.md) and [setup](https://github.com/TanushV/OpenDialer#readme).

0.2.1 fixes Windows in-place upgrades that could leave the old backend executable locked. Installer cleanup is restricted to the exact installation path; a resource drop guard also stops the owned process tree during the updater's early-exit path. Existing workspace data remains in place. The signed Windows 0.1.1→0.2.1 upgrade and Mac 0.2.0→0.2.1 update pass native version/data-preservation checks. See the validation record for exact source and receipts.

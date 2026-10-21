OpenDialer 0.3.1 — developer preview; installed-update validation in progress.

0.3.1 replaces 0.3.0, which was withdrawn the day it was published. On Windows, 0.3.0 could not open a workspace it had just migrated from 0.2.1: the upgrade flushed its retained recovery copy through a read-only file handle, which Windows rejects (macOS accepts it), so every start stopped before the workspace opened. The installed data stayed intact. 0.3.1 flushes through a writable handle, and the upgrade, lock and journal suites now also run on Windows in CI. Nothing else changed from 0.3.0.

0.3.1 is the lean completion of the calling workflow:
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

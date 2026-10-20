Found **six actionable harness defects** by static inspection. No concrete defect found in the scoped `744944a` repairs or the database-cgroup repair.

1. **MSI updater exit can falsely pass.** [windows_native_smoke.py:257](/Users/tanushv/Documents/Codex/2026-10-14/im/work/OpenDialer/scripts/windows_native_smoke.py:257) counts only `OpenDialer-*-installer.exe`. The pinned updater launches MSI updates through `msiexec.exe`. Once files match, the probe can report zero updaters while MSI installation is still running and reopen prematurely. **Correction:** identify and await the scoped MSI updater as well; retain the exact backend-hash check.

2. **MSI acceptance uses NSIS payload provenance.** [windows-acceptance.yml:67](/Users/tanushv/Documents/Codex/2026-10-14/im/work/OpenDialer/.github/workflows/windows-acceptance.yml:67) extracts the expected backend exclusively from NSIS, including the MSI matrix leg. A manifest missing its MSI entry can fall back to generic NSIS and satisfy that hash without proving an MSI upgrade. **Correction:** extract from the selected installer format and verify the matching published updater artifact before testing.

3. **Review can modify the wrong call.** [windows_native_smoke.py:204](/Users/tanushv/Documents/Codex/2026-10-14/im/work/OpenDialer/scripts/windows_native_smoke.py:204) always selects the first review row. Baseline and candidate simulation calls share a fixed timestamp; history breaks ties by descending UUID. When the baseline UUID sorts first, the current journey overwrites its note and subsequently verifies that wrong call. **Correction:** capture the journey’s call identity and select/assert that exact conversation throughout restart verification.

4. **Follow-up saving races a stale element.** [windows_native_smoke.py:380](/Users/tanushv/Documents/Codex/2026-10-14/im/work/OpenDialer/scripts/windows_native_smoke.py:380) reads the element captured before saving. [FollowupTasks.tsx:257](/Users/tanushv/Documents/Codex/2026-10-14/im/work/OpenDialer/apps/desktop/src/components/FollowupTasks.tsx:257) keys rows by ID and version, so refresh remounts the row. If refresh completes before that read, Selenium raises `StaleElementReferenceException`. **Correction:** capture identity before saving, then reacquire and verify the updated row.

5. **Recovery-screen check waits for nonexistent text.** [windows_native_smoke.py:411](/Users/tanushv/Documents/Codex/2026-10-14/im/work/OpenDialer/scripts/windows_native_smoke.py:411) performs a case-sensitive wait for `Recovery`; the actual heading is `Work needing recovery`. The ordinary successful journey times out there. **Correction:** target the existing section’s accessible label or exact heading.

6. **Cleanup can leave owned descendants running.** [frozen_acceptance.py:56](/Users/tanushv/Documents/Codex/2026-10-14/im/work/OpenDialer/scripts/frozen_acceptance.py:56) treats root-process exit as tree completion. On POSIX, a parent that exits on SIGTERM with a descendant that ignores it causes `wait()` to succeed, skipping SIGKILL and leaving the descendant alive. An already-exited launcher also bypasses cleanup entirely. **Correction:** retain ownership-safe descendant/group tracking, verify their exit, and finish cleanup before recording success.

The production trace supports same-call serialization, same-lead preservation, cancellation cleanup, lease-owner fencing, lost-response/version handling, and supersession without erasing genuine provider errors. The cgroup repair reads the connected PostgreSQL backend PID’s actual membership and limits, addressing the prior stale-manifest finding.

Actual packaged/native execution remains **unverified**, separately from these code defects. This review establishes no funded semantics, Windows device/trust, or UI acceptance. No tests, builds, services, providers, installed-data inspection, or UI automation were performed; no files were edited.

Entry and exit values were identical:

```text
HEAD:
fa168a2d6bdb10a5cc48a572c9039e3bad25407c

scripts.inspect_architecture.digest(source_files()):
7356d74e931be82c633c645e1da5015441f3f5fb138c53b04560d6cf1c83af01
```

Concurrent parent changes appeared in acceptance tooling/docs; findings above remain anchored to the requested commits. Bounded review stopped.


One concrete defect remains in finding 4. Findings 1–3 appear repaired in the inspected source and regression tests; tests were read, not executed.

**P2 — Reference acceptance still trusts the database’s recorded resource limits.**  
[scripts/measure_completion.py:188](/Users/tanushv/Documents/Codex/2026-10-14/im/work/OpenDialer/scripts/measure_completion.py:188)

Reproducible sequence: collect a valid manifest, then increase the database container’s CPU/memory limits before measurement. Its network namespace, PostgreSQL `system_identifier`, database and port remain unchanged. The gate checks actual cgroup limits only for the API process, so it accepts the stale database limits and can certify an actual deployment exceeding 2 vCPU/4 GiB.

The database identity also identifies the cluster rather than its executing container: a replacement PostgreSQL process using the same data directory and retained shared network namespace can satisfy those checks under different resource limits.

**Smallest correction:** bind the connected PostgreSQL process to fresh host-inspected container/cgroup membership and effective limits at measurement admission. Add a regression retaining identical PostgreSQL/network identity while changing the database’s executing resource limits. Current tests mutate identity fields but do not cover this case.

The other targeted repairs are present:

- Duplicate hangups fence completion by originating call/request generation and captured SDK identity; delayed audio cleanup checks ownership after awaiting.
- Subscribed invalidations and `reloadCall` share a read generation; current-call identity prevents restoring the previous call.
- Maintenance/job errors remain in load/shift receipts, including after recovery; final queue states are checked. Unconfigured inference and normal source coalescing are not evidence of paid-provider failure.

Host manifest collection, elapsed profiles, native updates/hardware, live carrier/provider behavior and semantic quality remain unverified gates. Their pending runs are not code defects.

No files were edited or prohibited operations performed. Fingerprints were unchanged:

```text
Entry HEAD: e731b554c9473e9d2a3c7703212eaf97ec7f10c0
Exit HEAD:  e731b554c9473e9d2a3c7703212eaf97ec7f10c0

Entry/exit production digest:
77e3a88544c366a96e2d11903ba41c6ad41ee3b48801c16d8d5f0966b49e8a6c
```

Digest computed read-only with `scripts.inspect_architecture.digest(source_files())`.
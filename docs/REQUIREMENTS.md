# Minimum and comfortable hardware

[Install](INSTALL.md) · [Documentation](README.md)

Rechecked October 26, 2026. **The previous 4-core / 8–16 GB figures were generic planning estimates,
not measured OpenDialer requirements.** Component measurements now cover a packaged backend, production UI
in WebKit, call processing and separate ML training. They still do not establish a certified minimum computer.

## What to use

| Platform | Minimum / lower-end candidate | Comfortable starting point |
| --- | --- | --- |
| Mac | Apple Silicon and macOS 14+ are package constraints. An 8 GB M1 is a useful lower-end test machine, but has not been tested here. | Apple Silicon with 8 GB RAM for focused use; more RAM depends on other apps, not an established 16 GB OpenDialer requirement. |
| Windows | A supported x64 Windows 11 CPU with 2 cores and 4 GB RAM is the **OS floor**, not a proven OpenDialer minimum. This is the first configuration to test. | 8 GB RAM on a supported x64 PC is a provisional starting point. A 4-core CPU gives training/background work more room; it is not a demonstrated calling requirement. |
| Shared server | No validated minimum or rep-capacity figure. | Size against team concurrency and measured latency. The earlier 4-vCPU / 8-GB suggestion was unmeasured and is withdrawn as a requirement. |

**Comfortable figures are provisional recommendations, not a passed hardware matrix.** Do not buy an upgrade
solely to satisfy the old 16 GB guidance. Measure the intended computer with your normal browser/CRM workload first.

Windows' floor comes from [Microsoft's OS requirements](https://learn.microsoft.com/en-us/windows/whats-new/windows-11-requirements);
processor eligibility and the other Windows requirements still apply. The [base M1 MacBook Air](https://support.apple.com/en-us/111883)
has 8 GB RAM. Neither manufacturer's specification is an OpenDialer performance test.

No dedicated GPU is needed: language-model inference is remote and statistical ML is CPU-based.
Current source also bundles Codex for optional ChatGPT login import. The earlier measurements below exclude
that subprocess; its inference-time memory and CPU have not yet been profiled with a live account.
The trainer limits numerical-library threads to one. Two cores on different CPU generations are not interchangeable,
so a GHz/core count alone cannot establish responsiveness.

Other requirements:

- Current downloads: Apple Silicon Mac and Intel/AMD x64 Windows. No Intel Mac, Windows ARM-native or Linux desktop package.
- Microphone/headset and stable internet. A local workspace also needs reachable HTTPS webhooks while calling.
- At least 1024 × 720 usable display pixels.
- Allow 2 GB free disk initially, plus workspace history and backups. This remains a planning allowance, not an installer-space test.
- No separate Python, Node, Rust or database installation for desktop users. WebView2 is included in the Windows installer.

## How minimum and comfortable requirements are validated

There is no universal conversion from one process's peak memory to a PC RAM requirement.
A defensible result needs a defined workload, acceptance thresholds and the **actual candidate hardware/OS**.
This follows [Microsoft's performance-testing guidance](https://learn.microsoft.com/en-us/azure/well-architected/performance-efficiency/performance-test).
On macOS, inspect memory pressure, compression and swap activity as well as process memory;
[Apple explains why free RAM alone is insufficient](https://support.apple.com/guide/activity-monitor/view-memory-usage-actmntr1004/mac).

For OpenDialer, use this test matrix:

1. Test the release build in a disposable workspace on each candidate Mac/Windows machine. Record CPU model,
   cores, OS, physical RAM, app version and background applications. A CPU throttle or an address-space limit
   on an M5 is not a substitute for an older CPU or a 4 GB machine.
2. Measure launch, a fresh workspace, 10k-lead import, 100k retained attempts, navigation, a calling session,
   post-call analysis, normal and maximum admitted ML training, and recovery after training.
3. Collect the whole app process group: shell, webview renderer/network/GPU helpers, authority and trainer.
   Separate startup/import peaks, settled idle and foreground/background overlap. Report physical footprint
   and CPU-time deltas; 100% CPU in these measurements means one fully occupied core.
4. Run at least 40 samples for short hot paths. Use the existing local targets: next-lead p95 ≤100 ms,
   webhook receipt ≤30 ms, event application/wrap-up ≤100 ms and workspace ≤150 ms. On a team server use
   the hosted targets and actual rep concurrency. UI and startup need separate native measurements.
5. Repeat complete runs on a quiet candidate machine, then with the user's normal CRM/browser workload.
   Include a sustained calling session and monitor memory growth, paging, audio glitches and dropped calls.
   The minimum passes normal operation without exhaustion; comfortable hardware also passes the representative
   multitasking workload with resource headroom. A short sample cannot establish a leak-free all-day session.
6. Publish the lowest configuration that actually passed, the workload and the limits of the evidence.
   Real headset/carrier audio and remote model latency require permissioned live tests.

## Measurement results

Test host: **Apple M5, 10 cores, 32 GiB RAM, macOS 26.5.1**. Source `833a471`, rebuilt frozen backend/trainer,
production UI in Playwright WebKit 2370, and disposable SQLite data. The UI used browser mode against the local authority.
This is a **component profile**, not a measurement of the installed native application.

| Phase | Median combined footprint | Sampled peak | Mean CPU (100% = one core) |
| --- | --- | --- | --- |
| Fresh workspace, idle UI | 182.2 MiB | 246.2 MiB | 5.8% |
| 100k history, background analysis | 395.9 MiB | 548.8 MiB | 22.1% |
| Navigation through six main pages | 544.7 MiB | 654.8 MiB | 49.5% |
| Synthetic WebRTC loopback | 486.2 MiB | 538.4 MiB | 17.7% |
| 40 simulated call journeys + loopback | 574.1 MiB | 654.9 MiB | 105.5% |
| 600-row trainer + workspace polling + loopback | 715.2 MiB | 763.9 MiB | 84.2% |
| 20k-row trainer + workspace polling + loopback | 738.4 MiB | 865.4 MiB | 65.2% |
| 60 seconds after training; loopback still running | 644.4 MiB | 646.0 MiB | 16.0% |

Footprints are summed across the backend, WebKit browser/renderer/network/GPU helpers and trainer when present.
OS services and the native Rust shell are not included. Approximately one-second sampling can miss shorter peaks;
these figures are not a RAM cap. CPU timestamps were calibrated against Python process CPU time and converted from
Mach ticks with this host's 125/3 timebase. A 105.5% mean means about **1.06 M5 cores**, not 105.5% of the whole Mac.

The workload imported 10,000 leads, loaded 100,000 historical attempts, navigated the main pages and completed
40 accelerated simulated call journeys. Both training artifacts passed checksum verification. The 600-row dataset
fitted the human stage; the 20,000-row dataset fitted all three funnel stages. Their elapsed times were **62.6 s** and
**57.9 s** including packaged-trainer startup. Different data complexity and shared-host contention mean these times
are not a scaling curve. Twenty thousand rows is the input row limit, not a proof of worst-case feature complexity.

The two-peer WebRTC loopback carried about 286 seconds of synthetic audio per received stream with zero reported
packet loss, but 27 concealment events per stream. This proves media processing ran; it does **not** establish glitch-free
hardware/carrier audio. Output was muted and no microphone or prospect call was used.

### Responsiveness did not fully pass

| Local HTTP path | Measured p95 | Existing target | Result |
| --- | --- | --- | --- |
| Next lead | 534.0 ms | ≤100 ms | Miss |
| Event application | 84.6 ms | ≤100 ms | Pass |
| Wrap-up | 149.1 ms | ≤100 ms | Miss |
| Workspace | 265.6 ms | ≤150 ms | Miss |

Workspace p95 during training was **1,013.4 ms** (600 rows) and **713.5 ms** (20k rows), also above target.
Backend restart to HTTP health took **5.61 seconds**; this is different from the old in-process database-reopen measurement.
There were no UI page errors or failed simulated call journeys, but functional completion does not erase latency misses.
The machine had elevated memory pressure and approximately **38.7 GiB swap already in use before the run**.
Neither the misses nor changing host swap can be attributed solely to OpenDialer from this experiment.

The post-training footprint remained around 644 MiB rather than returning to the fresh-workspace baseline.
The workspace now held imported history and warm caches, and the loopback was still active. This short run establishes
neither a memory leak nor an all-day stable ceiling.

[Full measurement receipt](evidence/lean/resource-profile-20261026.json).
The earlier [209 MiB backend-only spot check](evidence/lean/resources-20261026.json) omitted UI, media and training;
it must not be used as the application's minimum RAM requirement. This follow-up is one complete profile, not a
repeated pass on minimum-spec hardware. The first exploratory run had a probe navigation error and was excluded.


## What remains unverified

- The native Tauri shell, clean-machine Mac/Windows installation, actual Windows WebView2 resource use and hardware audio.
- 4 GB / 8 GB candidate machines, older CPUs and a quiet dedicated hosted server. Only this 32 GB M5 was available for this run.
- Actual Telnyx media, transcription and funded remote inference; simulated calls do not measure provider latency.
- Long-session stability and comfortable performance with a defined multitasking workload.

The installed application and its data were not modified. Do not interpret an untested row as a passed minimum.

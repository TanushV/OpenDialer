# System requirements and sizing

[Install](INSTALL.md) · [Documentation](README.md)

Checked October 26, 2026. Platform constraints come from the release assets and build configuration.
CPU/RAM recommendations below are planning guidance: **a lowest-working machine has not been certified**.

## Desktop

| Item | Requirement or recommendation |
| --- | --- |
| Mac | Apple Silicon (M-series); macOS 14.0+ is the configured deployment floor. Current download is ARM64, not Intel/universal. |
| Windows | Intel/AMD x64. Prefer Windows 11; Windows 10 compatibility has not been certified on a clean machine. No ARM-native installer. |
| CPU | Apple Silicon, or a modern Intel/AMD CPU with 4 cores recommended. No measured minimum clock speed. |
| RAM | 8 GB system RAM recommended; 16 GB if you keep many browser tabs or other demanding apps open. Not an app memory reservation. |
| GPU | No dedicated GPU required. Language-model inference runs through remote providers; statistical ML uses CPU. |
| Free disk | Allow 2 GB initially for installation, extraction and updates, plus data and backups. This is a planning allowance, not a measured installer minimum. |
| Display | At least 1024 × 720 usable pixels (the configured minimum window size); more room is preferable. |
| Audio/network | Microphone and headphones/headset; stable internet for calling, transcription and AI. No offline calling. |

The desktop bundles its backend and trainer. End users do not install Python, Node, Rust or PostgreSQL.
Windows installers include the offline WebView2 installer; see [Tauri's runtime documentation](https://v2.tauri.app/start/prerequisites/).
Linux and Intel Mac desktop packages are not currently published.

## Shared server

Start planning a small team deployment around **4 vCPU and 8 GB RAM**, SSD storage, persistent volumes and backups.
This is a starting estimate, not an established rep-capacity guarantee. The server runs the API, background worker,
PostgreSQL and HTTPS proxy. Team size, preparation volume, retained history and training affect requirements.

The earlier capped 2-vCPU test completed eight-rep journeys but missed the next-lead latency target
(p95 about 0.8–1.2 seconds) on an oversubscribed host. A later uncapped run passed latency targets;
neither establishes capacity on a dedicated server. Benchmark the intended host and workload before production.
See [deployment](../deploy/runbooks/deploy.md) and [dated validation](VALIDATION.md).

## What we measured

A source-backend benchmark on October 26 used a **10-core Apple M5, 32 GiB RAM, macOS 26.5.1**,
Python 3.12 and a disposable SQLite database: **10,000 leads, 100,000 historical attempts, five samples per path**.

| Measurement | Result |
| --- | --- |
| Peak process resident memory | 218,906,624 bytes (208.8 MiB) |
| CPU time, including fixture creation | 53.35 seconds user + 20.43 seconds system |
| Elapsed time, including fixture creation | 115.36 seconds |
| Database size | 306,163,712 bytes (292.0 MiB) |
| Database reopen/startup benchmark | 67 ms |
| Next-lead p95 / workspace p95 | 81.9 ms / 60.9 ms |

[Machine-readable receipt](evidence/lean/resources-20261026.json).
This measures one Python benchmark process, **not total installed-app memory**. It excludes the native window/webview,
live WebRTC audio, funded inference and concurrent trainer work. Fixture creation is included in peak memory and CPU time.
Five timing samples are a spot check, not a capacity certification. Other work was running on the Mac;
CPU time is not an idle-utilization figure or minimum CPU specification. Windows runtime usage was not measured.

The statistical trainer runs as a separate temporary CPU process with numerical-library threads limited to one.
Training input is bounded at 20,000 rows / 16 MiB, but these input limits are **not** peak-RAM limits.
Allow headroom for training and the operating system. The packaged backend does not load the trainer's fitting libraries.

### Reproduce the backend measurement (macOS, source checkout)

After [development setup](DEVELOPMENT.md):

```sh
/usr/bin/time -l .venv/bin/python scripts/bench.py --leads 10000 --attempts 100000 --samples 5
```

This uses temporary data and simulated providers, with no real calls or purchases. For meaningful performance comparisons,
use the normal 40 samples on an otherwise quiet machine. The benchmark does not launch or modify the installed app.

# Transcription comparison — October 28, 2026

October 29 decision: the owner selected direct Telnyx/Soniox WebSocket streaming and two-second automatic coaching updates. The current implementation proposal is [MANAGED_WEBHOOK_PLAN.md](MANAGED_WEBHOOK_PLAN.md). The recommendations below record the earlier comparison; they are not an instruction to implement Groq or OpenRouter transcription.

Research and a bounded Groq API test only. No runtime implementation, provider configuration, live carrier calls, release, or installed-app changes. The supplied Groq key was entered through a non-echoing prompt, held in the benchmark process, and not saved in these files. The owner intends to rotate it after testing.

## Findings

- Groq Whisper Large V3 Turbo processes completed clips much faster than playback. This does not establish lower live latency than the current Telnyx/Soniox streaming path.
- Current OpenDialer explicitly selects `Soniox` and starts separate inbound transcription streams for prospect and agent. It does not pin a Soniox model/version or tune endpointing in `telephony/workflows.py`.
- Chunk collection dominates a low-latency Whisper integration. A 2-second buffer plus the measured approximately 0.25-second request takes about 2.25 seconds from buffer start to result. Individual words wait between near-zero and the full chunk duration before submission. Phrase-based flushing also adds endpoint-detection delay. Overlap/context can improve recognition but increases processing and billing.
- OpenRouter has real STT models, including cheaper provider routes for Whisper Turbo and inexpensive Qwen ASR models. These are candidates, not tested quality/latency winners. A model supporting streaming does not prove that OpenRouter exposes live bidirectional audio streaming for it.

## Measured Groq experiment

[Raw sanitized receipt](evidence/lean/groq-stt-20261028.json).

Thirty successful HTTP requests: five samples, two models, three repetitions per combination. Sequential requests through a reused HTTP client; model order alternates each repetition; language English, temperature zero, verbose JSON. No retries. Timing includes upload/network/server response, but excludes audio collection, local endpoint detection and downstream coaching. No steady-state concurrency or reliable p95 claim follows from this small test.

| Input sample | Turbo median request | Large V3 median request |
|---|---:|---:|
| 2-second synthetic prefix | 249 ms | 276 ms |
| 5-second synthetic prefix | 243 ms | 281 ms |
| 10-second synthetic prefix | 295 ms | 349 ms |
| 21.556-second complete synthetic speech, 8 kHz | 299 ms | 429 ms |
| 11-second public JFK sample | 302 ms | 342 ms |

Turbo's slowest request was 540 ms; Large V3's was 447 ms. The initial Turbo request included the first connection establishment. This is a local HTTP microbenchmark, not engine-only processing speed. The longer synthetic clip ran about 72 times faster than playback end-to-end; Groq's advertised speed factor describes a different benchmark.

Both models preserved the full sample's explicit opt-out, appointment correction and dollar amounts in all three repeats. Turbo spelled the synthetic name `Priya` as `Pria`; Large V3 preserved `Priya`. The 2/5/10-second prefixes cut through speech, so their outputs cannot establish how a continuous chunked conversation would stitch together. A single synthetic voice and famous human speech do not establish performance on noisy, accented, interrupted two-speaker calls. Resampling to 8 kHz does not reproduce PSTN codecs or network loss. No aggregate accuracy/WER ranking is claimed.

Estimated list-rate cost: Turbo $0.0020852 plus Large V3 $0.00578643, total **$0.00787163**, applying Groq's 10-second minimum per request. This is an estimate, not a read-back of the account bill or free allowance.

## Published price comparison

USD per input audio hour, before account discounts, taxes, credit-purchase fees, silence removal, overlapping windows, carrier charges and any minimum billing. These are transcription prices only. Two separately billed continuous speaker streams can double one wall-clock conversation hour; verify Telnyx's actual account billing granularity before forecasting the invoice.

| Path | Published base price per audio hour | Qualification |
|---|---:|---|
| Telnyx Soniox, current engine | $0.12 | $0.002/min; streaming call integration already exists |
| Telnyx Parakeet | $0.09 | $0.0015/min; documented final-only output changes interim behavior |
| Groq Whisper Large V3 Turbo | $0.04 | Minimum 10 billed seconds/request |
| Groq Whisper Large V3 | $0.111 | Same minimum; slightly slower in this test |
| OpenRouter Whisper Turbo via DeepInfra | approximately $0.012 | API price $0.00000333/sec; Groq route remains approximately $0.04/hour |
| OpenRouter Qwen3 ASR 0.6B via DeepInfra | approximately $0.012 | API price $0.00000333/sec; not inference-tested |
| OpenRouter Qwen3 ASR 1.7B via DeepInfra | $0.027 | API price $0.0000075/sec; not inference-tested |

Groq Turbo non-overlapping continuous chunks cost approximately $0.20/audio-hour at 2 seconds/request, $0.08 at 5 seconds/request, and $0.04 at 10 seconds/request. The 10-second billing minimum is not a requirement to submit at least 10 seconds of audio: the short clips succeeded. Silence suppression can lower billed input but also requires correct local speech detection and context handling. Do not assume other providers have Groq's billing floor or no floor; test their usage receipts.

OpenRouter's current transcription documentation says routing `order`, `only` and `ignore` preferences are not applied to STT requests. Therefore the cheapest Whisper endpoint is not a guaranteed bill for every request. The current endpoint catalog lists both DeepInfra and Groq for Turbo. It lists DeepInfra for the two Qwen candidates. Confirm actual provider and cost in response usage before adopting a budget. OpenRouter's displayed cross-workload latency statistics are not comparable to these fixed-input local tests.

## Telnyx speed and the evidence still needed

Soniox is a streaming recognizer: provisional text appears during speech, followed by stable/final output. Actual OpenDialer latency includes recognition finalization, Telnyx webhook emission/delivery, the receiver path, local processing and the helper's model call. None of that complete carrier path was measured in this experiment.

Soniox documents endpoint controls with a 500–3,000 ms configurable maximum endpoint delay (2,000 ms default in its current direct documentation). That is a configuration bound, not typical latency, not a promise about Telnyx's engine configuration and not an end-to-end network bound. Telnyx publishes a standalone STT latency harness; its example Deepgram measurements are not measurements of this app's Soniox webhook path. A matched comparison needs the same audio, real-time pacing, first-interim and stable-final timestamps, and the actual configured carrier route.

Do not drop interim transcripts merely to improve cost/throughput: `privacy.py::_transcript_in` currently uses interim remote opt-out language to create a temporary hold. The live helper uses finalized chunks. Changing to chunked batch recognition or final-only Parakeet needs equivalent behavior tests.

## Recommendation

Keep the current streaming path while choosing the replacement. Groq Turbo is a promising inexpensive chunked-transcription candidate, especially where several seconds of delay are acceptable; this test does not establish that it beats streaming Soniox for live assistance. For a lower-cost route with the user's existing OpenRouter key, benchmark Whisper Turbo and Qwen3 ASR on the same representative audio before deciding. Prefer accuracy and stable-final delay over advertised faster-than-playback factors.

Any direct PC-to-Groq/OpenRouter transcription would keep speech traffic out of Supabase, but it requires local audio capture, chunking, speaker mapping and transcript integration changes. It is not entirely local inference and still uses internet bandwidth. No such implementation has started.

## Sources checked

- [Groq STT models, pricing, request limits and minimum billing](https://console.groq.com/docs/speech-to-text)
- [Telnyx STT rate card](https://telnyx.com/pricing/speech-to-text)
- [Telnyx supported transcription engines](https://developers.telnyx.com/docs/voice/programmable-voice/speech-to-text)
- [Soniox real-time transcription](https://soniox.com/docs/stt/rt/real-time-transcription)
- [Soniox endpoint detection](https://soniox.com/docs/stt/rt/endpoint-detection)
- [Telnyx standalone STT latency harness](https://github.com/team-telnyx/stt-latency-test)
- [OpenRouter STT API and routing restrictions](https://openrouter.ai/docs/guides/overview/multimodal/stt)
- [OpenRouter Whisper Turbo providers](https://openrouter.ai/openai/whisper-large-v3-turbo)
- [OpenRouter Qwen3 ASR 0.6B](https://openrouter.ai/qwen/qwen3-asr-0.6b)
- [OpenRouter STT model catalog](https://openrouter.ai/api/v1/models?output_modalities=transcription)
- [Public human-speech test asset](https://github.com/ggml-org/whisper.cpp/blob/master/samples/jfk.wav)

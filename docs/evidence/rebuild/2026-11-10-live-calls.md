# Live checks on the 2.0.0 candidates, November 10, 2026

The installed app on the owner's Mac, driven in the background. Calls go only to the owner's test line, 203-821-1101,
inside a test session (Settings, Test phone, Test call). Times are Pacific.

## Candidate 1 (draft built from `f5f79b4`)

Installed 20:35 over 1.0.10 after checking the archive's SHA-256, its signature (`codesign --verify --deep --strict`,
the same ad-hoc signature as 1.0.10; the updater checks its own signature) and that no call was live. The backend came
up on 2.0.0 with the workspace intact; Settings read Ready to call with Telnyx, the call connection, 5 numbers, the
ChatGPT login and OpenRouter all checked.

| Step | Result |
|---|---|
| Test call, Start calling | Audio sat on "Checking microphone and registering" for several minutes with no way out. The new build is a new app to macOS, which held the microphone request on its permission prompt while nobody was at the Mac. **Defect**: the request had no time limit. |
| After the prompt cleared | "No one left to call right now", blaming calling hours. **Defect**: the owner's test phone was an older lead (November 5) still held as `delivery_test`, so the queue never offered it. Calling hours were not the cause; a test call to the owner's own line already ignores them. |

Fixed in `e25e4ba` with tests: the microphone request waits at most 30 seconds, says "click Allow" after 4 and then
pauses with where to turn the microphone on; starting a test session clears any hold on the test lead (an opt-out
still stops it).

## Candidate 2 (draft built from `3a35613`)

Installed 22:06 over candidate 1 the same way (checksum, signature, no live call); healthy on 2.0.0.

| Step | Result |
|---|---|
| Settings, End test session, Test call | The Dialer opened at its top (the scroll fix) with the test session on. |
| Start calling | Each new build is a new app to macOS, so it asks for the microphone again. The Dialer now says "Waiting for the microphone. If macOS asks, click Allow." after 4 seconds instead of waiting silently. Nobody was at the Mac to click Allow. |
| Test call 1 (22:08) | Once the microphone was allowed, audio connected and the call rang 203-821-1101. Answered after 8 seconds; the carrier reported a human 5 seconds later. **L1 passed**: Hang up from the app ended the call with one hang-up request; the Dialer showed "You ended the call" and the six outcome keys. Saved as Not interested. |
| After saving | **Defect**: the test session ended on save and the Dialer started "Next call in 3 seconds" from the real lists. Paused at once; no prospect was dialed (all were outside calling hours). |

Fixed with tests: a saved or skipped test call pauses calling (the Dialer stops, and the backend pauses the rep when it
ends the session), and a call to the rep's own typed test phone stays a test call after its outcome is saved (it was
relabeled real, which would have sent it to learning and HubSpot).
| Test call 2 (22:29) | **L2 passed**: the owner answered and hung up after 25 seconds; the Dialer showed "They hung up · 0:25" (carrier: hang-up by the callee) with the outcome keys and no false block. Paused first, then saved; nothing else dialed. |
| Test call 3 (22:30), owner asked not to answer | Answered after 7 seconds; Telnyx premium answering-machine detection reported a human 4 seconds later, so the Dialer stayed on the line until the far end hung up after 32 seconds ("They hung up · 0:32"). If the owner's voicemail took it, detection missed the greeting and L5 (skip at detection) did not happen on this call; the owner's word settles which. The screen then went out of reach (locked), so the outcome was left unsaved; the Dialer waits for it and dials nothing. |

## The missing live transcript (owner report, November 10)

The owner saw no transcript on the test calls, and the Dialer had said "Live transcription stopped for this call".
Telnyx speech-to-text itself works with the owner's key (a spoken test sentence came back word for word). The cause is
in the app: auto-dial starts each call from a timer, not a click, and WebKit only starts an audio engine from the rep's
own click or key, so the engine that streams call audio to the transcriber stayed suspended and capture gave up at once.
Manual dialing (0.8.x) always had a click, which is why transcripts worked then. The backend also swallowed the
failure without a log line.

Fixed with tests: Start calling unlocks one audio engine that every call of the session reuses; a capture that still
stops says why (the Dialer sends the reason and the backend logs it), and the speech socket logs its own failures.

The live helper's script tracking matched the rep's words to script sections, which a paraphrase or a speech-to-text
error breaks. It is replaced by something simple: the script goes to the model as numbered parts, and each tip starts
with the number of the part that fits the moment; the Dialer shows the line to say and that script part together.

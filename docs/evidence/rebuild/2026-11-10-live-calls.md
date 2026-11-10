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

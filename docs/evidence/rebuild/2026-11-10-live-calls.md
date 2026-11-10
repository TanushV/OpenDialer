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

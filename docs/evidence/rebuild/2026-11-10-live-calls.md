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

## The real cause: a dead audio leg reused (found November 10, 23:40)

The store showed all three calls tied to the rep's audio leg `v3:6_Gbj…`, created **November 6** and still marked
`answered` because its hang-up event arrived while the app was closed. Starting audio reused any leg the store called
active, and the Dialer trusted the store's "answered", so each call was bridged to audio no client held: the
transcript had no streams (the stopped-transcript message), and the rep's side was almost certainly silent.

Fixed with tests: a leg is reused only if this run of the app made it and the carrier says it is live; any other is
ended and a fresh leg dialed. The Dialer dials only once its own audio client holds the leg. The earlier "audio needs
a click" theory was wrong (the app's web view allows audio without one); that change stays as harmless.

## Candidate 3 (draft built from `015805a`)

Installed 00:38 (November 10) over candidate 2: archive SHA-256 matched `SHA256SUMS.txt`, `codesign --verify --deep
--strict` passed, the installed binary matches the archive, and no call was live. The backend came up healthy on the
same workspace.

| Step | Result |
|---|---|
| Reopen with test call 3 still owed an outcome | The Dialer reopened on "They hung up · 0:32" with the outcome keys; nothing dialed (L10's "reopening shows the call owed an outcome; no second dial" holds across an app replacement). |
| Save it (key 6) | Saved as Voicemail, still marked a test call. The owner then said no voicemail was left, so the saved outcome is wrong; it is a test record that reaches neither learning nor HubSpot. Something answered after 7 seconds, the carrier heard a human, and it hung up after 32 seconds of silence (the rep's audio was the dead leg). The likeliest answerer is the phone's own call screening for unknown numbers, which a carrier cannot tell from a person; L5 (voicemail skipped at detection) is still untested. |
| After saving | The Dialer showed Start calling, paused; no countdown into the real lists (the candidate 2 defect is fixed live). |

## HubSpot, checked again November 10

The saved Service Key reaches portal 51457381 (the portal the owner's other system writes to) and sees all 4,759
contacts, including the test contact. The portal has no lists: the list search returns 0 with no filter, with every
processing type, and with an empty body. The app pulls leads only from a chosen list (section 9B), so until a list
exists it syncs no one from HubSpot. L14 to L18 wait on a list.

## HubSpot segment "testing" and the contact data, November 10 (later)

HubSpot now calls lists "segments"; the API is unchanged. The owner made a manual contact segment "testing" (id 14,
50 contacts). On a scratch copy of the workspace (relay and session files left out, only the CRM job queue running, no
spend limit, so nothing bought and no research spend):

| Check | Result |
|---|---|
| List read | The app lists the segment (`[{"id": "14", "name": "testing"}]`); the real membership read, untested until now, works |
| L14 sync | 50 added in 0.6 seconds with names and companies; every lead "needs_number" ("This lead has no number yet."), none callable |
| L14 sync again | 0 added, 0 updated, 0 removed |

The contacts: 4,759, every one lifecycle "lead" and written by the owner's other system. One has a phone number (the
test contact). 1,481 carry that system's qualification fields: status `eligible` 996, `irrelevant` 249,
`account_rejected` 163, `relevant_unreachable` 42, `stale_or_conflicted` 31; eligible bands `a_plus` 240, `a` 258,
`b` 249. So a segment filtered on a phone number holds one contact; a useful segment filters on qualification, and the
app must find numbers (the waterfall, which runs only under a spend limit set on that list) before anyone is callable.

Not testable by calling the owner: a call to the owner's line is always a test call, and test calls are never written
to HubSpot, so L15 to L18 cannot come from a real carrier call without dialing a prospect. The write path itself was
proven against this portal on November 9.

Test call 3's carrier events: answered 6 seconds after dialing, premium detection `human_residence` 3.3 seconds later,
hung up by the far end at 32 seconds (`normal_clearing`, SIP 200), no voicemail left. A pickup that fast with no
message is the pattern of a phone's own screener for unknown numbers, which a carrier hears as a person.

## Candidate 4 (draft built from `6b437f3`, Phase 11)

Installed 03:36 (November 10) over candidate 3: archive SHA-256 matched, signature verified, the installed binary
matches the archive, no call was live, and the bundle carries no trainer (StatML archived). The backend came up
healthy on the same workspace.

| Check | Result |
|---|---|
| R11.2 live | Within a second of starting, the app re-read Telnyx and finished setting up the number ending 158 (bought November 6, stuck as "purchased"): now `limited_real_use`, `active`, route confirmed. All six numbers are ready. |
| R11.3 live | Not yet: the Mac's screen was locked, so the app's window never loaded, its call client did not register and it did not report itself reachable. A callback now would get the greeting and a follow-up, as designed for a closed app. Needs the owner at the Mac (microphone Allow for the new build). |

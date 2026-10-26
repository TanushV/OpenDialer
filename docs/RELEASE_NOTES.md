# OpenDialer 0.5.1

- Remove prerecorded voicemail drop from calling, script preparation and policy settings. The drop API is removed and previously queued outbound playback/gather commands are blocked. Existing in-flight cleanup and opt-out handling remain available.
- New workspaces default to 8 a.m.–9 p.m. in each prospect’s timezone, including daylight-saving changes. The start is inclusive; no new call starts at or after 9 p.m. Existing published campaign hours are preserved and can be changed in Settings.
- Voicemail outcomes and optional next-call advancement remain available. No prerecorded message is sent.

Updates from 0.4.x and 0.5.x retain the workspace. Versions 0.2/0.3 start a fresh workspace; old data stays untouched in its previous folder and is not migrated.

Updater signatures verify artifact integrity. Apple notarization, Windows publisher signing, clean-machine installation and live carrier/audio behavior remain separate gates.

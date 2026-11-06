# OpenDialer 0.9.2

- **Fix planning for mixed locations.** Leads whose state and phone indicate different time zones can be scored by hour without a planner error. Every plausible local calling window still applies.
- **Stop outbound at 5 p.m.** New calls and requested callbacks respect a hard 17:00 cutoff in the lead's local time, including older saved policies. Existing conversations can finish and inbound callbacks remain answerable.
- **Choose your calling lists.** Dial from all active lists, one list or several; due callbacks outside that pool are separate explicit choices. Selection stays with your workspace and rep across reloads.
- **Manage lists and leads.** Lists show preparation, callable-now, held, completed and callback counts. Pause/resume, rename and archive/restore lists; select leads in bulk to pause, archive, move between lists or correct location. History, notes, opt-outs and caller-ID assignments remain.
- **Keep one contact across lists.** Reimporting the same phone in the campaign links another membership and reuses its history and preparation. List changes invalidate saved capacity plans; renamed lists keep their preparation budget scope.

Updating from 0.9.0 or 0.9.1 keeps your workspace and saved credentials. macOS may ask for microphone access again.
Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched in its previous folder.
Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 0.8.7

- **Inbound calls ring the ready owner.** Owners and admins use the same workspace SIP login as **Go ready**.
- **Faster inbound greetings and transfers.** Fresh calls can route to a rep or play the callback greeting without a carrier status lookup.
  The app still checks for stale calls and stops when the caller hangs up.

Updating from 0.8.6 keeps your workspace and saved credentials without OS password prompts.
Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched in its previous folder.
Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

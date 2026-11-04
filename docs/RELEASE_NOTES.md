# OpenDialer 0.8.5

- **Voicemail.** Telnyx Premium AMD detects voicemail while the rep is bridged on answer. A confirmed beep ends an
  ordinary attempt immediately. Choose **Stay** during the greeting to keep the call open. Final attempts stay open
  for the rep to leave a message. Reps answer any screening prompts they hear.
- **Ring time.** Outbound ringing defaults to 40 seconds and can be changed in Numbers. Connected call duration is separate.
- **Live connect report.** Numbers shows company totals, weekly and rolling 28-day trends and breakdowns by caller number
  and receiving carrier. Wrap-up can confirm two-way audio, record screening or audio problems and exclude test calls.
  Bridge timing is based on provider events. Short calls and early drops have their own warnings.
- **Number health.** New routes have a 24-hour outbound hold. The business-day ramp runs at 15, 25, 40 and 50 attempts per
  number per day within the owner’s limit. Repeated SIP 603/603+ blocks pause outbound and keep callbacks working.
- **Free registration.** Export inventory for Free Caller Registry and save manual label checks in Number evidence.
  Administrators can explicitly approve a single metered carrier and line-type lookup from a lead packet.

Updating from 0.8.4 keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace
and old data stays untouched in its previous folder. Credentials use a private local key file without OS password prompts.
Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.
Live carrier delivery and connect-rate measurement are rollout steps.

# OpenDialer 0.7.1

- **Fixes from running 0.7.0 on a real list.** The Leads page shows a list's research progress right after import
  instead of after the first lead finishes. Agents no longer skip a lead over its phone number; the app checks numbers
  before calling. When an agent skips a lead, the list and the lead show why. The AI card in Settings shows Done once
  ChatGPT and the live help key are set, and its text no longer squeezes beside the button. Plainer Leads table
  labels. The updater shows each release's own notes.
- **Practice is the coaching list.** Each item says what to work on and how, and the call review agent updates it
  after each call. The AI rehearsal box is gone.
- **Live help keeps more good tips.** A tip whose model audit cites a source the app does not know now gets the full
  independent review instead of being dropped.
- **One script, pasted once.** Paste or open your script as Markdown on the Script page. It is checked as you type,
  with every problem listed by line. Sections: Opening, Screening, Discovery and Close are required; Gatekeeper,
  Offers, Objections, Facts and Never say are optional. Bullets are options the AI picks for each lead, and
  `{{ instruction }}` marks text the AI writes for each lead. The old typed template fields are gone.
- **Every lead is researched and gets its own script before it is called.** Importing a list starts AI agents in
  threads of leads (contacts at one company stay together). Each agent searches the web, saves facts with the page
  and an exact quote (checked against the page), then writes the lead's script under close-out rules: valid picks,
  every written line citing a fact, no Never-say phrases, no unsupported numbers or promises. Leads are called only
  after their preparation closes. The Leads page shows progress per list. On ChatGPT this runs on GPT-6.1 Terra at
  maximum reasoning effort, included in your plan.
- **Several contacts at one company:** they are never called back to back (the next calling day at the earliest),
  and once one books, the others are held. Correcting the booking releases them.
- **Simpler Settings:** one page with Calling, AI, Script, Leads, Calling hours and Do-not-call list; everything else
  is under More settings. Connect ChatGPT with one click when Codex is signed in on this computer. The AI budget is
  now set per lead list and never blocks calling.
- **Coaching is one list** on the Review page. Follow-ups moved to Review.
- **Live help on the real Cerebras route works end to end:** independent reviews and call summaries no longer fail
  on OpenRouter, and a supported question stays visible when only its explanation is rejected.
- Removed: scheduled prepaid DNC screening (the manual prepaid check stays), the separate ChatGPT plan sign-in, and
  the rolling preparation settings.

Updates from 0.4.x through 0.6.x keep the workspace. Your existing script, if any, needs to be pasted again in the
new format; until then leads are prepared once a script is saved. Desktop credentials use a private local key file
without OS password prompts; upgrades from 0.6.0/0.6.1 may require you to re-enter keys and re-import setup/login
files. Versions 0.2/0.3 start a fresh workspace; old data stays untouched in its previous folder and is not migrated.

Updater signatures verify artifact integrity. Apple notarization, Windows publisher signing, clean-machine
installation and live carrier/audio behavior remain separate gates.

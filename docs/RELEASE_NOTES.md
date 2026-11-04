# OpenDialer 0.8.3

- **Numbers set themselves up.** Approving a purchase or adding a number now confirms its route, turns Telnyx recording
  off, applies your daily spend cap and caller name, and starts warmup. There is no Set up route click unless something
  failed.
- **Add numbers you already own.** Numbers, Check my Telnyx numbers lists your whole account, and you add the ones you
  want. A number on another Telnyx app shows a warning, because adding it moves its calls here. Nothing is used until
  you add it. Pause outbound remains how you exclude one.
- **One test call for the workspace.** One answered call proves the shared call path for every number, and it no longer
  expires after 30 days. A number could previously stop dialing with no clear reason.
- **Voicemail is the only automatic hang-up.** The system hangs up an ordinary attempt only at a voicemail beep Telnyx
  detects, and never before 7 seconds, because Telnyx penalizes very short calls. The final attempt stays on the line so
  the rep can leave a message. Call screening and unclear results stay with the rep. Detection is now Telnyx's iPhone
  screening aware option.
- **Search by city.** Each result shows its city, and naming a city tries the area code's overlays and keeps only that
  city, so San Francisco finds San Francisco 628 numbers rather than Marin.
- **Warmup and limits.** New numbers start at 15 real calls a day and rise to 60 with use. Numbers shows the real limit.
- **Same metro first.** A lead gets a number from its metro before a less used one elsewhere in the state. Overlays count
  as one metro (415 and 628; the New York City codes).
- **Callbacks.** Numbers asks where returned calls should go. The spoken message names your business, and callers who are
  not on a lead hear it without ringing a rep.
- **Clearer refusals.** Next call says when a number is the problem: no number, the test call, paused numbers, a state
  with no number, or today's limit.
- **Caller name is optional.** One saved name applies to every number. Leave it blank for none.
- **Daily spend cap.** Telnyx refuses new calls past the cap, $25 a day unless you change it.
- **One transcription switch.** Live transcription and help are now one setting. The app still keeps no call audio.

# OpenDialer 0.8.2

- **Number search finds nearby numbers.** When an area code is sold out at Telnyx (415 is), the search now offers
  numbers from the same area in the same state, such as the 628 overlay for San Francisco, marked "near 415". Numbers
  from other states are never offered. Before, a sold-out area code showed "No priced numbers found".
- **Better objection help (0.8.1).** When a prospect objects, live help now sees every objection response in your script
  (not just the first four), plus your approved talk tracks and reviewed examples, and is told to adapt the closest
  response to the prospect's words, acknowledge, ask one clarifying question and use a listed fact only if it
  answers the concern. A stray "(nothing)" marker no longer shows at the end of an answer.
- **Calling needs no paperwork.** Business, campaign and carrier approval records no longer gate calls. The national
  do-not-call check is optional and off by default (Settings, Do-not-call check); your own opt-outs, holds and calling
  hours always apply. Leads without a time zone column are callable: the app uses every zone their state and area code
  allow and only calls when all are inside your hours.
- **Readiness says what to do next.** Instead of one SIP message, Check readiness and Go ready name the single next
  step: connect Telnyx, import this computer's audio login, add a caller number, set up its route, or call it once to
  test callbacks.
- **Existing Telnyx numbers.** Sync finds numbers you own that are not routed anywhere yet; numbers routed to another
  Telnyx app are counted and left alone. After you set up a number's route, calling it once from your own phone and
  answering in OpenDialer starts its warmup automatically.
- **Warmup.** A new number starts at 5 real calls per rolling 24 hours and may grow by at most 20% a day, only after
  20 resolved calls across 10 leads and 3 calling days, never past the number's maximum. Waiting alone earns nothing.
  Two reps calling at once never share a number that is reserved or on a call.
- **Caller name.** Numbers has one action that lists your business name for outbound caller ID with Telnyx (the paid
  inbound name lookup is never turned on) and a link to the Free Caller Registry.
- **Call timing.** Ordinary retries wait at least a day. A requested callback is due at its own time (a five-minute
  callback is no longer blocked by the old one-hour cooldown) and is completed when you reach the person. When the
  next-to-last attempt goes unanswered, your next Space press offers that lead once more right away (for five minutes);
  nothing dials by itself.
- **Voicemail.** Every call asks Telnyx to detect answering machines. A machine on an ordinary attempt ends the call;
  on the final attempt the line stays open and the app shows the voicemail wording for you to say.
- **Follow-up calls.** Calls after the first open with a This call section from the lead's real history: a fresh
  introduction after missed calls, the promise for a requested callback, last conversation's summary, objections and
  open questions, and the voicemail wording on the final attempt. Scripts may add optional Follow-up and Voicemail
  sections.
- **Live help is a conversation.** New speech gets a short streamed reply about what to say or ask next; silence gets
  nothing, a slower reply is not cut off by new speech, and observations are saved about once a minute so long calls
  keep their early context. Replies only advise; they never change records.
- **Fixes from running 0.7.0 on a real list.** The Leads page shows a list's research progress right after import
  instead of after the first lead finishes. Agents no longer skip a lead over its phone number; the app checks numbers
  before calling. When an agent skips a lead, the list and the lead show why. The AI card in Settings shows Done once
  ChatGPT and the live help key are set, and its text no longer squeezes beside the button. Plainer Leads table
  labels. The updater shows each release's own notes.
- **Practice is the coaching list.** Each item says what to work on and how. After each call the review agent adds
  one or two points from that call ("Work on … How …"). The AI rehearsal box is gone.
- **Call summaries cover the whole call.** The post-call agent now sees speech already handled during the call, so
  the summary includes what the prospect said, and quotes with typographic dashes or apostrophes still match.
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

Updates from 0.4.x through 0.7.x keep the workspace. Your existing script, if any, needs to be pasted again in the
new format; until then leads are prepared once a script is saved. Desktop credentials use a private local key file
without OS password prompts; upgrades from 0.6.0/0.6.1 may require you to re-enter keys and re-import setup/login
files. Versions 0.2/0.3 start a fresh workspace; old data stays untouched in its previous folder and is not migrated.

Updater signatures verify artifact integrity. Apple notarization, Windows publisher signing, clean-machine
installation and live carrier/audio behavior remain separate gates.

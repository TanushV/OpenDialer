"""The one answer to "can I dial?" (section 4 rule 2, R4.3, G4).

`blockers(facts)` lists what stops calling now, most basic first, each with a code, a plain message and the place to
fix it. Dialing, the queue and Settings all read this list; no other readiness gate exists. A blocker that software can
clear (a reconnecting relay, audio coming back) clears itself on the next read.
"""

from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str
    fix: str  # Where the rep fixes it: settings, numbers, leads, dialer or attention.
    # Promised to clear by itself, was repaired once and is still here (R9E.6): the screen shows the real control.
    stuck: bool = False

    def detail(self):
        found = {"code": self.code, "message": self.message, "fix": self.fix}
        return {**found, "stuck": True} if self.stuck else found


STUCK = "This did not clear by itself. Press Reconnect; if it stays, quit and reopen OpenDialer."


def mark_stuck(found, stuck_codes):
    """Blockers the watchdog could not clear say so and name what to do, instead of "clears by itself"."""
    return [
        Blocker(b.code, f"{b.message} {STUCK}", "settings" if b.fix == "dialer" else b.fix, stuck=True)
        if b.code in stuck_codes
        else b
        for b in found
    ]


CATALOG = {
    "DATABASE_INTEGRITY_FAILURE": (
        "The local database failed its integrity check. Restore a backup in Settings.",
        "settings",
    ),
    "RESTORE_RECONCILIATION_REQUIRED": ("Finish checking the restored backup in Settings.", "settings"),
    "TELNYX_NOT_CONFIGURED": ("Add your Telnyx API key in Settings.", "settings"),
    "WEBHOOK_SIGNATURE_KEY_MISSING": ("Import your calling setup file in Settings.", "settings"),
    "AUDIO_SETUP_MISSING": (
        "This Mac has no calling audio login. Import your calling setup file in Settings.",
        "settings",
    ),
    "CONNECTION_SETUP_CHANGING": (
        "Calling setup is being changed; new calls resume when it is saved.",
        "settings",
    ),
    "CONNECTION_SIGN_IN_REQUIRED": (
        "Call events need a new sign-in. Import your setup file in Settings.",
        "settings",
    ),
    "CONNECTION_IN_USE_ELSEWHERE": (
        "Your calling setup is active on another computer. Quit OpenDialer there; this one takes over within a minute.",
        "settings",
    ),
    "CONNECTION_RECONNECTING": (
        "Call events are reconnecting. This clears by itself; keep OpenDialer open.",
        "dialer",
    ),
    "CONNECTION_DELIVERY_GAP": (
        "Call events are catching up after a gap. This clears by itself in a few seconds.",
        "dialer",
    ),
    "NO_CAMPAIGN": ("Import a lead list to start a campaign.", "leads"),
    "NO_CALLER_NUMBER": ("Add a caller number in Numbers: buy one, or add one you own.", "numbers"),
    "CALLER_NUMBER_ROUTE_REQUIRED": ("In Numbers, choose Set up route on your caller number.", "numbers"),
    "CALLER_NUMBER_CALLBACK_TEST_REQUIRED": (
        "One test call left: call {phone} from your own phone and answer it here. Every number opens after that.",
        "numbers",
    ),
    "CALLER_NUMBER_PAUSED": ("Your caller numbers are paused. Review them in Numbers.", "numbers"),
    "PENDING_LOCAL_OPTOUTS": (
        "An opt-out is still being saved. Calling resumes when it is stored.",
        "dialer",
    ),
    "TEST_SESSION_TEST_LINE_ONLY": (
        "A test session dials only your own test line. Turn test sessions off in Settings to call leads.",
        "settings",
    ),
    "TELNYX_CALL_LIMIT_REACHED": (
        "Telnyx account call limit reached: every line on the account is in use. Calling resumes by itself in a minute. "
        "If this keeps happening, ask Telnyx support to raise the account's calls-at-once limit.",
        "dialer",
    ),
    "REP_AUDIO_NOT_READY": (
        "Connecting your audio. Check your headset or microphone if this stays.",
        "dialer",
    ),
}


@dataclass(frozen=True)
class Exit:
    """How a blocker ends (G10). Every blocker has at least one way out the rep can see or that needs nobody."""

    by_itself_within: int | None = (
        None  # Seconds within which software clears it; None if only a control does.
    )
    control: str | None = None  # The visible control that clears it, where one does.


EXITS = {
    "DATABASE_INTEGRITY_FAILURE": Exit(control="Settings: Restore a backup"),
    "RESTORE_RECONCILIATION_REQUIRED": Exit(control="Settings: Finish checking the restored backup"),
    "TELNYX_NOT_CONFIGURED": Exit(control="Settings: Telnyx API key"),
    "WEBHOOK_SIGNATURE_KEY_MISSING": Exit(control="Settings: Import setup file"),
    "AUDIO_SETUP_MISSING": Exit(control="Settings: Import setup file"),
    "CONNECTION_SETUP_CHANGING": Exit(by_itself_within=60),
    "CONNECTION_SIGN_IN_REQUIRED": Exit(control="Settings: Import setup file"),
    "CONNECTION_IN_USE_ELSEWHERE": Exit(by_itself_within=90),
    "CONNECTION_RECONNECTING": Exit(by_itself_within=60),
    "CONNECTION_DELIVERY_GAP": Exit(by_itself_within=60),
    "NO_CAMPAIGN": Exit(control="Leads: Import a list"),
    "NO_CALLER_NUMBER": Exit(control="Numbers: Buy or add a number, or ask the pool operator to add one"),
    "CALLER_NUMBER_ROUTE_REQUIRED": Exit(control="Numbers: Set up route"),
    "CALLER_NUMBER_CALLBACK_TEST_REQUIRED": Exit(
        control="Call the number from your phone and answer it here"
    ),
    "CALLER_NUMBER_PAUSED": Exit(control="Numbers: Resume outbound, or ask the pool operator to resume it"),
    "PENDING_LOCAL_OPTOUTS": Exit(by_itself_within=60),
    "TEST_SESSION_TEST_LINE_ONLY": Exit(control="Settings: End test session"),
    "TELNYX_CALL_LIMIT_REACHED": Exit(by_itself_within=60),
    "REP_AUDIO_NOT_READY": Exit(by_itself_within=30, control="Dialer: Check audio"),
}
# Blockers that clear by themselves; Review does not list them as something for the rep to fix.
SELF_CLEARING = {
    "CONNECTION_RECONNECTING",
    "CONNECTION_DELIVERY_GAP",
    "REP_AUDIO_NOT_READY",
}
# Order of `CATALOG` is the order shown: the first blocker is the one to fix first.
ORDER = {code: index for index, code in enumerate(CATALOG)}


@dataclass(frozen=True)
class Facts:
    """What `blockers()` needs, read fresh each time by the caller."""

    carrier_setup_required: bool = True  # False when the carrier connection is the replay carrier.
    integrity_ok: bool = True
    restore_hold: bool = False
    telnyx_configured: bool = True
    signature_key: bool = True
    audio_setup: bool = True
    connection: str | None = None  # None when ready, else a CONNECTION_* code.
    campaign: bool = True
    number: str | None = None  # None when a caller number is ready, else its code.
    test_number: str | None = None  # The number to call for the one-time test call.
    shared_pool: bool = False
    pending_optouts: int = 0
    rep_audio_ready: bool | None = None  # None when the question is not being asked (Settings).
    # Telnyx refused the last dial over the account's channel limit (R9C.4).
    channel_limited: bool = False


def blockers(facts):
    codes = []
    if not facts.integrity_ok:
        codes.append("DATABASE_INTEGRITY_FAILURE")
    if facts.restore_hold:
        codes.append("RESTORE_RECONCILIATION_REQUIRED")
    if facts.carrier_setup_required:
        if not facts.telnyx_configured:
            codes.append("TELNYX_NOT_CONFIGURED")
        if not facts.signature_key:
            codes.append("WEBHOOK_SIGNATURE_KEY_MISSING")
        if not facts.audio_setup:
            codes.append("AUDIO_SETUP_MISSING")
    if facts.connection:
        codes.append(facts.connection)
    if not facts.campaign:
        codes.append("NO_CAMPAIGN")
    if facts.number:
        codes.append(facts.number)
    if facts.pending_optouts:
        codes.append("PENDING_LOCAL_OPTOUTS")
    if facts.channel_limited:
        codes.append("TELNYX_CALL_LIMIT_REACHED")
    if facts.rep_audio_ready is False:
        codes.append("REP_AUDIO_NOT_READY")
    return [
        blocked(code, facts.test_number, shared=facts.shared_pool)
        for code in sorted(set(codes), key=ORDER.__getitem__)
    ]


def blocked(code, phone=None, *, shared=False):
    message, fix = CATALOG[code]
    if shared:
        message, fix = {
            "NO_CALLER_NUMBER": (
                "Ask the pool operator to add a caller number to the shared pool.",
                "numbers",
            ),
            "CALLER_NUMBER_PAUSED": (
                "Ask the pool operator to resume outbound on a shared number.",
                "numbers",
            ),
            "CALLER_NUMBER_CALLBACK_TEST_REQUIRED": (
                "Start a Test call in Settings, then call the displayed number back and answer it here.",
                "settings",
            ),
        }.get(code, (message, fix))
    return Blocker(code, message.format(phone=phone or "one of your numbers"), fix)


def test_line_refusal(phone, *, test_session, test_lines):
    """R6.6: during a test session any number but the owner's test line is refused, in code."""
    return [blocked("TEST_SESSION_TEST_LINE_ONLY")] if test_session and phone not in test_lines else []


def refuse(found):
    """Stop a dial or queue request with the first blocker, if any."""
    if found:
        raise HTTPException(409, found[0].detail())

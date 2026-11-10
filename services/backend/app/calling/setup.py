"""The Settings rows (section 7, R7.1 to R7.3): what to add, where to get it, what it powers, and whether it works.

A row is green only after a real check. Network checks (Telnyx, OpenRouter, ChatGPT and the optional services) are
stored with a fingerprint of the value they checked, so changing a key makes its row unchecked again. Facts the app
can see at any time (the call connection, numbers, script and calling hours) are read fresh.
"""

import hashlib
import json
from dataclasses import dataclass

OK, MISSING, UNCHECKED, FAILED, OPTIONAL = "ok", "missing", "unchecked", "failed", "optional"


@dataclass(frozen=True)
class Spec:
    id: str
    label: str
    where: str
    link: str | None
    powers: str
    fields: tuple[str, ...]  # Saved values the row's check depends on.
    optional: bool = False


SPECS = (
    Spec(
        "telnyx",
        "Telnyx API key",
        "Telnyx portal: API keys",
        "https://portal.telnyx.com/#/api-keys",
        "Calls, numbers, transcription",
        ("telnyx_api_key",),
    ),
    Spec(
        "connection",
        "Call connection",
        "Automatic; import the setup file only when disconnected",
        None,
        "Call events",
        (),
    ),
    Spec("numbers", "Phone numbers", "Numbers page: buy, or add one you own", None, "Caller ID", ()),
    Spec(
        "chatgpt",
        "ChatGPT login",
        'Run "codex login" in Terminal, then use this Mac\'s login',
        None,
        "Research and scripts",
        (),
    ),
    Spec(
        "openrouter",
        "OpenRouter key",
        "OpenRouter: Settings, Keys",
        "https://openrouter.ai/settings/keys",
        "Live helper, summaries, fallback agent",
        ("openrouter_api_key",),
    ),
    Spec("test_phone", "Test phone", "Type your own number", None, "Test call button", ("test_phone",)),
    Spec("script", "Script", "Script page", None, "Every call", ()),
    Spec("hours", "Calling hours", "Edit in place", None, "When calls are allowed", ()),
    Spec(
        "realvalidito",
        "RealValidito key and secret",
        "RealValidito console",
        "https://app.realvalidito.com",
        "Do-not-call check on CRM leads",
        ("realvalidito_api_key", "realvalidito_api_secret"),
        optional=True,
    ),
    Spec(
        "hubspot",
        "HubSpot Service Key",
        "HubSpot: Settings, Integrations, Service Keys",
        None,
        "List sync and call logging",
        ("hubspot_service_key",),
        optional=True,
    ),
    Spec(
        "treg",
        "Treg token",
        "treg org agent-new opendialer --cap <daily calls>",
        None,
        "Waterfall on CRM leads",
        ("treg_token",),
        optional=True,
    ),
)
BY_ID = {spec.id: spec for spec in SPECS}
# Rows whose state is read fresh from the app rather than from a stored network check.
LIVE = frozenset({"connection", "numbers", "chatgpt", "script", "hours", "test_phone"})


def fingerprint(spec, config):
    values = json.dumps([config.get(field) for field in spec.fields], sort_keys=True)
    return hashlib.sha256(values.encode()).hexdigest()


def check_record(spec, config, ok, detail, at):
    """What to store after a network check of `spec` against the saved `config`."""
    return {"ok": bool(ok), "detail": detail, "at": at, "fingerprint": fingerprint(spec, config)}


def row(spec, config, checks, live):
    """One row: `state` is ok, missing, unchecked, failed or optional, with `detail` saying why."""
    base = {
        "id": spec.id,
        "label": spec.label,
        "where": spec.where,
        "link": spec.link,
        "powers": spec.powers,
        "optional": spec.optional,
    }
    if spec.id in LIVE:
        ok, detail = live.get(spec.id, (False, "Not checked yet"))
        return {**base, "state": OK if ok else (OPTIONAL if spec.optional else FAILED), "detail": detail}
    if not all(config.get(field) for field in spec.fields):
        return {**base, "state": OPTIONAL if spec.optional else MISSING, "detail": "Not set"}
    check = checks.get(spec.id)
    if not check or check.get("fingerprint") != fingerprint(spec, config):
        return {**base, "state": UNCHECKED, "detail": "Saved, not checked yet"}
    return {
        **base,
        "state": OK if check["ok"] else FAILED,
        "detail": check["detail"],
        "checked_at": check["at"],
    }


def rows(config, checks, live):
    return [row(spec, config, checks, live) for spec in SPECS]

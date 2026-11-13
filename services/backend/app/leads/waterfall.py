"""The number waterfall for CRM leads (D19, section 9A), ported from the outside runner's agreed rules.

Per contact:
  1. A usable number on the CRM record skips the waterfall and goes to do-not-call screening.
  2. LeadOcean, then AI Ark, then Tomba, with a 10-cent stop on the stage.
  3. Wiza last, outside the stop, only when nothing was accepted. A miss is free.
Per number:
  A free format check rejects malformed and non-US numbers before Trestle is paid.
  Trestle: yes is Confirmed and stops; blank is Unconfirmed and stops; no, invalid or grade F moves on.
  At most 2 numbers per provider reach Trestle. There is no paid live-line check.
Nothing passes: a Trestle "no" on every number is Conflict (dialable by hand only); otherwise No valid number.

Every paid request is written to the lead's ledger with its idempotency key before it is sent, and a settled answer is
reused by fingerprint, so a crash or a repeat sync never buys the same answer twice (R9B.14, R9B.16).
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta

from app.leads.hubspot_sync import dialable_phone

LEADOCEAN, AIARK, TOMBA = "leadocean.people.phone.find", "aiark.people.phone.find", "tomba.people.phone.find"
FINDERS = [LEADOCEAN, AIARK, TOMBA]
WIZA = "wiza.people.phone.find"
POLL = "wiza.people.reveal.get"
FORMAT = "hlrlookup.people.phone.validate"
TRESTLE = "trestleiq.people.contact.verify"
# Treg catalog prices in micro-USD and methods, read November 6, 2026. A price change shows up as a charge above the
# quote, which disables that endpoint for the run (R15).
QUOTES = {LEADOCEAN: 5_000, AIARK: 26_335, TOMBA: 44_500, WIZA: 150_000, TRESTLE: 30_000, FORMAT: 0, POLL: 0}
METHODS = {
    LEADOCEAN: "GET",
    AIARK: "POST",
    TOMBA: "GET",
    WIZA: "POST",
    TRESTLE: "GET",
    FORMAT: "POST",
    POLL: "GET",
}
FINDER_STOP = 100_000
MAX_CHECKS = 2
ACCEPT = {"confirmed", "unconfirmed"}
REPLAY_WINDOW = timedelta(hours=23)
OFFICE = {"office", "work", "landline", "main"}


class SpendLimit(Exception):
    """The list's spend limit would be passed. The contact stays open and resumes after the limit is raised."""


class Held(Exception):
    """A paid request cannot be settled safely (unknown charge, failed check, stale replay). Nothing more is bought."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def normalize(number):
    number = re.sub(r"[ ()\-.]", "", str(number or "").strip())
    if number.startswith("00"):
        number = "+" + number[2:]
    if not number.startswith("+"):
        if re.fullmatch(r"\d{10}", number):
            number = "+1" + number
        elif re.fullmatch(r"1\d{10}", number):
            number = "+" + number
    return number if re.fullmatch(r"\+[1-9]\d{6,14}", number) else None


def request(endpoint, c):
    url, name, domain = c.get("linkedin_url"), c.get("name"), c.get("domain")
    if endpoint == LEADOCEAN:
        return {"linkedin_url": url} if url else None
    if endpoint == AIARK:
        return {"linkedin": url} if url else ({"name": name, "domain": domain} if name and domain else None)
    if endpoint == TOMBA:
        return {"linkedin": url} if url else ({"email": c["email"]} if c.get("email") else None)
    if endpoint == WIZA:
        person = {"profile_url": url} if url else {"full_name": name, "domain": domain}
        return (
            {"individual_reveal": person, "enrichment_level": "phone"} if url or (name and domain) else None
        )
    return None


def _office_last(phones):
    return sorted(phones, key=lambda p: str(p.get("type", "")).lower() in OFFICE)


def candidates(endpoint, body):
    """Every number a finder returned, best first, normalized and de-duplicated."""
    raw = []
    if endpoint == LEADOCEAN:
        data = body.get("data") or {}
        listed = [data["best"]] if isinstance(data.get("best"), dict) else []
        listed += [p for p in data.get("phones") or [] if isinstance(p, dict)]
        raw += [p.get("phone") for p in _office_last(listed)]
    elif endpoint == AIARK:
        data = body.get("data")
        if isinstance(data, dict):
            for group in data.get("data") or []:
                raw += group if isinstance(group, list) else [group]
    elif endpoint == TOMBA:
        data = body.get("data")
        for item in data if isinstance(data, list) else [data] if isinstance(data, dict) else []:
            if isinstance(item, dict):
                raw.append(item.get("e164_format"))
    elif endpoint == WIZA:
        data = body.get("data") or {}
        raw += [
            p.get("number")
            for p in _office_last([p for p in data.get("phones") or [] if isinstance(p, dict)])
        ]
        raw += [data.get("mobile_phone"), data.get("phone_number")]
    out = []
    for number in raw:
        normal = normalize(number) if isinstance(number, str) else None
        if normal and normal not in out:
            out.append(normal)
    return out


def found(endpoint, body, status):
    """'candidate', 'miss' or 'error' for a finder's answer."""
    if status == 404 and endpoint == LEADOCEAN:
        return "miss"
    if not 200 <= status < 300 or not isinstance(body, dict):
        return "error"
    if candidates(endpoint, body):
        return "candidate"
    data = body.get("data")
    if endpoint == LEADOCEAN and isinstance(data, dict) and data.get("found") is False:
        return "miss"
    if endpoint in (AIARK, TOMBA) and "data" in body and data in (None, [], {}):
        return "miss"
    if endpoint == AIARK and isinstance(data, dict) and not data.get("data"):
        return "miss"
    if (
        endpoint == WIZA
        and isinstance(data, dict)
        and (data.get("status") == "finished" or data.get("is_complete"))
    ):
        return "miss"
    return "error"


def identity(url):
    match = re.search(r"linkedin\.com/in/([^/?#]+)", str(url or "").lower())
    return match[1] if match else None


def identity_conflict(body, contact):
    """An explicitly different returned profile is rejected, without guessing from name variants."""
    mine = identity(contact.get("linkedin_url"))
    if not mine or not isinstance(body, dict):
        return False
    for record in (body, body.get("person"), body.get("data")):
        if not isinstance(record, dict):
            continue
        for field in ("linkedin_url", "linkedin", "profile_url"):
            value = record.get(field)
            if isinstance(value, str) and value:
                if field == "linkedin" and re.fullmatch(r"[A-Za-z0-9_%.-]+", value):
                    value = "https://www.linkedin.com/in/" + value
                if identity(value) != mine:
                    return True
    return False


def format_result(body):
    rows = body.get("results") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None, {}
    r = rows[0]
    if r.get("error") == "BAD_FORMAT" or r.get("telephone_number_type") in {
        "INVALID",
        "UNKNOWN_INVALID",
        "BAD_FORMAT",
        "STAGE_AND_SCREEN",
    }:
        return False, r
    valid = r.get("error") == "NONE" and r.get("telephone_number_type") in {
        "MOBILE",
        "MOBILE_OR_LANDLINE",
        "LANDLINE",
        "FIXED_LINE",
        "FIXED_LINE_OR_MOBILE",
        "VOIP",
        "TOLL_FREE",
    }
    return (True if valid else None), r


class Waterfall:
    """One contact's run against a ledger dict (stored on the lead) and a Treg transport."""

    def __init__(self, ledger, treg, *, now, room_micro, save):
        self.ledger, self.treg, self.now, self.room, self.save = ledger, treg, now, room_micro, save
        ledger.setdefault("calls", [])
        ledger.setdefault("disabled", [])
        self.limit = FINDER_STOP

    def spent(self, *, run_only=False):
        calls = self.ledger["calls"]
        return sum(c.get("charged_micro", 0) for c in calls if not run_only or c.get("run") == self.run_id)

    def paid_for(self, endpoint, params):
        fp = digest([endpoint, params])
        return any(c["fingerprint"] == fp and c["state"] == "settled" for c in self.ledger["calls"])

    def can_spend(self, amount):
        """False means the stage stop. The list's own limit raises SpendLimit instead."""
        if self.spent(run_only=True) + amount > self.room:
            raise SpendLimit()
        return self.stage_spent + amount <= self.limit

    async def invoke(self, endpoint, params):
        if endpoint in self.ledger["disabled"]:
            raise Held(f"{endpoint} is disabled after a charge above its quote")
        fp = digest([endpoint, params])
        previous = [c for c in self.ledger["calls"] if c["fingerprint"] == fp]
        if previous and previous[-1]["state"] == "settled":
            return previous[-1]["response"]
        if previous:
            call = previous[-1]
            if self.now - datetime.fromisoformat(call["created_at"]) >= REPLAY_WINDOW:
                raise Held("An unsettled paid request is too old to replay safely; settle it in Treg first")
        else:
            call = {
                "endpoint": endpoint,
                "params": params,
                "fingerprint": fp,
                "quote_micro": QUOTES[endpoint],
                "key": str(uuid.uuid4()),
                "created_at": self.now.isoformat(),
                "state": "reserved",
                "run": self.run_id,
            }
            self.ledger["calls"].append(call)
            self.save()  # The reservation and its idempotency key are durable before the request is sent.
        answer = await self.treg.call(endpoint, METHODS[endpoint], params, call["quote_micro"], call["key"])
        meta = answer.get("_treg", {})
        charge = meta.get("charged_micro")
        if not isinstance(charge, int) or charge < 0:
            call["last_error"] = "unknown charge"
            self.save()
            raise Held("Treg did not report what this request cost; nothing more is bought")
        call.update(state="settled", charged_micro=charge, call_id=meta.get("call_id"), response=answer)
        if charge > call["quote_micro"]:
            self.ledger["disabled"].append(endpoint)
        self.save()
        self.stage_spent += charge
        return answer

    async def check(self, contact, number, provider):
        rec = {"number": number, "finder": provider}
        if not re.fullmatch(r"\+1[2-9]\d{9}", number):
            return {**rec, "verdict": "not_us_format"}
        answer = await self.invoke(FORMAT, {"telephone_number": number[1:], "output_format": "PLUS_E164"})
        if answer.get("_treg", {}).get("http_status", 0) not in range(200, 300):
            raise Held("The free format check failed")
        valid, details = format_result(answer.get("result"))
        rec.update(format_valid=valid, line_type=details.get("telephone_number_type"))
        if valid is False:
            return {**rec, "verdict": "bad_format"}
        country = (details.get("original_network_details") or {}).get("country_iso3")
        if country and country != "USA":
            return {**rec, "verdict": "not_us_format"}
        params = {"name": contact["name"], "phone": number}
        if not self.paid_for(TRESTLE, params) and not self.can_spend(QUOTES[TRESTLE]):
            return {**rec, "verdict": "budget_stop"}
        answer = await self.invoke(TRESTLE, params)
        if answer.get("_treg", {}).get("http_status", 0) not in range(200, 300):
            raise Held("The Trestle check failed")
        phone = (answer.get("result") or {}).get("phone") or {}
        rec.update(
            name_match=phone.get("name_match"),
            is_valid=phone.get("is_valid"),
            grade=phone.get("contact_grade"),
            activity=phone.get("activity_score"),
            trestle_line=phone.get("line_type"),
        )
        if phone.get("is_valid") is False or phone.get("contact_grade") == "F":
            rec["verdict"] = "bad_grade"
        elif phone.get("name_match") is False:
            rec["verdict"] = "conflict"
        else:
            rec["verdict"] = "confirmed" if phone.get("name_match") else "unconfirmed"
        return rec

    async def try_numbers(self, contact, numbers, provider, result, tried):
        checked = 0
        for number in numbers:
            if number in tried or checked == MAX_CHECKS:
                continue
            tried.add(number)
            rec = await self.check(contact, number, provider)
            if rec["verdict"] == "budget_stop":
                return None
            result["checks"].append(rec)
            if "name_match" in rec:
                checked += 1
            if rec["verdict"] in ACCEPT:
                return rec
        return None

    async def ask(self, contact, endpoint, result, tried):
        provider = endpoint.split(".")[0]
        params = request(endpoint, contact)
        answer = await self.invoke(endpoint, params)
        meta, body = answer.get("_treg", {}), answer.get("result")
        kind = found(endpoint, body, meta.get("http_status", 0))
        note = {
            "finder": provider,
            "kind": kind,
            "http": meta.get("http_status"),
            "charged": meta.get("charged_micro"),
        }
        result["notes"].append(note)
        if kind == "candidate":
            if identity_conflict(body, contact):
                note["identity_conflict"] = True
                return None
            return await self.try_numbers(contact, candidates(endpoint, body), provider, result, tried)
        return None

    async def run(self, contact):
        """Run the rules once. Returns {status, phone, finder, checks, notes, spent_micro}."""
        self.run_id = str(uuid.uuid4())
        self.stage_spent, self.limit = 0, FINDER_STOP
        result = {"checks": [], "notes": []}
        tried = set(self.ledger.get("wrong_numbers", []))
        accepted = None
        existing = dialable_phone(contact.get("existing_phone"))
        if existing and existing not in tried:
            return {
                **result,
                "status": "unconfirmed",
                "phone": existing,
                "finder": "crm",
                "spent_micro": self.spent(),
            }
        for endpoint in FINDERS:
            if accepted:
                break
            params = request(endpoint, contact)
            if params is None or endpoint in self.ledger["disabled"]:
                continue
            if not self.paid_for(endpoint, params) and not self.can_spend(QUOTES[endpoint] + QUOTES[TRESTLE]):
                break
            accepted = await self.ask(contact, endpoint, result, tried)
        params = request(WIZA, contact)
        if not accepted and params is not None and WIZA not in self.ledger["disabled"]:
            self.limit = self.stage_spent + QUOTES[WIZA] + MAX_CHECKS * QUOTES[TRESTLE]
            if not self.paid_for(WIZA, params):
                self.can_spend(QUOTES[WIZA] + QUOTES[TRESTLE])
            accepted = await self.ask(contact, WIZA, result, tried)
        conflicts = [c for c in result["checks"] if c["verdict"] == "conflict"]
        if accepted:
            status, phone, finder = accepted["verdict"], accepted["number"], accepted["finder"]
        elif conflicts:
            status, phone, finder = "conflict", conflicts[0]["number"], conflicts[0]["finder"]
        elif result["checks"] or any(n.get("identity_conflict") for n in result["notes"]):
            status, phone, finder = "no_valid_number", None, None
        elif any(n["kind"] == "error" for n in result["notes"]):
            status, phone, finder = "provider_error", None, None
        else:
            status, phone, finder = "no_valid_number", None, None
        return {**result, "status": status, "phone": phone, "finder": finder, "spent_micro": self.spent()}


def mark_wrong(ledger, number):
    """A `wrong_party` call stops that number; the next run reopens the contact and reuses paid answers."""
    ledger.setdefault("wrong_numbers", [])
    if number not in ledger["wrong_numbers"]:
        ledger["wrong_numbers"].append(number)

from datetime import datetime

from app.grounding import require_review, review_request


class ValidationFailure(ValueError):
    pass


VISIBLE_FIELDS = (
    "opener",
    "screening_intro",
    "gatekeeper",
    "discovery_questions",
    "proof",
    "closing_question",
    "objection_cards",
)


def script_review_request(script, facts, domain, seller_name, *, approved=None):
    approved = approved or {}
    fields = [
        {"field": key, "value": script[key], "evidence": script.get("fact_ids_used", [])}
        for key in VISIBLE_FIELDS
        if script.get(key) and script[key] != approved.get(key)
    ]
    if not fields:
        return None
    return review_request(
        "script",
        fields,
        [
            {
                "id": fact["fact_id"],
                **{
                    key: fact.get(key)
                    for key in ("statement", "source_id", "entity_domain", "expires_at", "status", "version")
                },
            }
            for fact in facts
            if fact["fact_id"] in script.get("fact_ids_used", [])
        ],
        {
            "lead_id": script.get("lead_id"),
            "domain": domain,
            "seller": seller_name,
            "approved": {key: approved.get(key) for key in VISIBLE_FIELDS},
        },
    )


def validate_script_evidence(script, facts, domain, seller_name, now):
    if (
        not script.get("lead_id")
        or not script.get("opener")
        or seller_name.lower() not in script.get("screening_intro", "").lower()
    ):
        raise ValidationFailure("Script identity or required ID missing")
    if len(script["opener"]) > 600 or len(script.get("screening_intro", "")) > 400:
        raise ValidationFailure("Script exceeds display limits")
    mapped = {f["fact_id"]: f for f in facts}
    for fact_id in script.get("fact_ids_used", []):
        f = mapped.get(fact_id)
        if (
            not f
            or f.get("status") != "supported"
            or f.get("entity_domain") != domain
            or not f.get("source_id")
        ):
            raise ValidationFailure("Missing or mismatched personalized evidence")
        if datetime.fromisoformat(f["expires_at"]) <= now:
            raise ValidationFailure("Expired personalized evidence")
    for claim in script.get("claims", []):
        if not claim.get("fact_ids") or not set(claim["fact_ids"]).issubset(
            set(script.get("fact_ids_used", []))
        ):
            raise ValidationFailure("Personalized claim lacks validated evidence")


def validate_script(script, facts, domain, seller_name, now, *, approved=None, review=None):
    validate_script_evidence(script, facts, domain, seller_name, now)
    request = script_review_request(script, facts, domain, seller_name, approved=approved)
    if request:
        try:
            require_review(request, review)
        except ValueError as exc:
            raise ValidationFailure(str(exc)) from exc
    return True

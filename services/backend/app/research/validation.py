import re
from datetime import datetime


class ValidationFailure(ValueError):
    pass


BANNED = re.compile(
    r"\b(guarantee(?:d)?|10x revenue|number (?:one|1) (?:google|ranking)|as we discussed|following up on our conversation|ignore (?:all |your )?instructions)\b",
    re.I,
)


def validate_script(script, facts, domain, seller_name, now):
    if (
        not script.get("lead_id")
        or not script.get("opener")
        or seller_name.lower() not in script.get("screening_intro", "").lower()
    ):
        raise ValidationFailure("Script identity or required ID missing")
    if len(script["opener"]) > 600 or len(script.get("screening_intro", "")) > 400:
        raise ValidationFailure("Script exceeds display limits")
    if BANNED.search(str(script)):
        raise ValidationFailure("Unapproved guarantee, relationship or instruction-like claim")
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
        if claim["text"].lower() not in " ".join(mapped[f]["statement"].lower() for f in claim["fact_ids"]):
            raise ValidationFailure("Claim not supported by cited fact text")
    return True

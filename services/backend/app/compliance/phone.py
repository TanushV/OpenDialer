from dataclasses import dataclass

import phonenumbers


@dataclass
class PhoneResult:
    e164: str | None
    extension: str | None = None
    format_status: str = "invalid"
    ownership_status: str = "unknown"
    assignment_evidence: str = "unknown"
    reachability_status: str = "unknown"


def normalize_phone(raw: str, country="US") -> PhoneResult:
    try:
        number = phonenumbers.parse(raw, country)
        if not phonenumbers.is_possible_number(number) or not phonenumbers.is_valid_number(number):
            return PhoneResult(None)
        return PhoneResult(
            phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164),
            number.extension or None,
            "valid",
        )
    except phonenumbers.NumberParseException:
        return PhoneResult(None)

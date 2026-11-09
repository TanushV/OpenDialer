"""Carrier refusals that name a cause the rep can act on (R9C.4).

Telnyx refuses a dial over the account's calls-at-once limit (Verified accounts get 10 channels; section 9C). The exact
error code is confirmed in L22; until then the refusal is recognised by its words, which every reported form shares.
"""

import json

LIMIT_WORDS = (
    "channel limit",
    "channels limit",
    "concurrent call",
    "concurrency limit",
    "too many concurrent",
)
PAUSE_SECONDS = 60


def is_channel_limit(status, body):
    """Whether a 4xx dial refusal says the account is out of call channels."""
    if not 400 <= status < 500:
        return False
    text = json.dumps(body).casefold() if not isinstance(body, str) else body.casefold()
    return any(words in text for words in LIMIT_WORDS)

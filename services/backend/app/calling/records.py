"""Typed records the calling package reads and writes (section 4 rule 4, R3.5).

Records this package owns forbid unknown fields. Records another package still owns in legacy code are read through a
`View` that names only the fields used here; each view becomes that package's own model when its phase rewrites it.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class View(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class AgentLeg(Record):
    kind: Literal["agent"]
    rep_id: str
    provider_id: str | None = None
    state: Literal["submitting", "uncertain", "ringing", "answered", "ended"]
    command_id: str | None = None
    lease_owner: str | None = None
    lease_until: str | None = None
    call_id: str | None = None
    ended_at: str | None = None


class Presence(Record):
    status: Literal["ready", "paused", "busy"] | None = None
    audio_ready: bool = False
    telephony_ready: bool = False
    agent_call_control_id: str | None = None
    pending_optouts: int = 0
    wrapup_call_id: str | None = None
    at: str | None = None


class UserView(View):
    presence: Presence = Presence()
    sip_username: str | None = None
    telephony_credential_id: str | None = None


class NumberView(View):
    phone: str
    lifecycle: str
    identity_configured: bool = False
    provider_present: bool | None = None


def presence_of(user_data):
    return UserView.model_validate(user_data).presence


class CallView(View):
    """The call fields dialing, hang-up and recovery read (R3.5, R4.5)."""

    state: str
    state_at: str | None = None
    submitted_at: str | None = None
    started_at: str | None = None
    answered_at: str | None = None
    ended_at: str | None = None
    provider_id: str | None = None
    max_call_seconds: int | None = None
    rep_id: str | None = None
    attempt_ordinal: int | None = None
    dial_scope: dict[str, Any] = {}


class ReservationView(View):
    """What a reservation carries into the dial it becomes."""

    dial_scope: dict[str, Any] = {}
    treatment_variant_id: str | None = None
    feature_snapshot_id: str | None = None
    policy_decision_id: str | None = None
    coaching_comparison: Any = None
    script_comparison: Any = None


class LeadAttempts(View):
    """A lead's attempt counters, read before a dial and restored when nothing reached the prospect."""

    attempt_count: int | None = None
    attempt_allowance: int | None = None
    last_attempt_at: str | None = None
    next_attempt_at: str | None = None
    pair_until: str | None = None
    pair_of: str | None = None


class CommandView(View):
    """A carrier command in the outbox: the durable intent behind every dial and hang-up."""

    kind: str | None = None
    call_id: str | None = None
    status: str | None = None
    prior_attempt: dict[str, Any] = {}
    lease_until: str | None = None
    sent_at: str | None = None


class PolicyLimits(View):
    max_attempts: int = 4


class LegView(View):
    kind: str | None = None
    call_id: str | None = None

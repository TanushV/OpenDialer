"""Typed records the calling package reads and writes (section 4 rule 4, R3.5).

Records this package owns forbid unknown fields. Records another package still owns in legacy code are read through a
`View` that names only the fields used here; each view becomes that package's own model when its phase rewrites it.
"""

from typing import Literal

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

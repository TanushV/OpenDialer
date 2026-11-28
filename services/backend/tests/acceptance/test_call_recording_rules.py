"""Call insights plan P1 to P4: what a call keeps, when, and how it is deleted.

The rep's side (audio and transcript) is kept on every answered call. The prospect's side is kept only when sure
one-party consent applies (their supplied state, if any, and their area code's state are both off the stricter
list), or from the moment the rep presses R after saying the call is recorded. Live transcription for the helper
runs on every call unless "Transcribe the prospect live in stricter states" is off. Audio is recorded on this
computer, encrypted, and kept until deleted; an opt-out deletes that phone's recordings.
"""

import io
from datetime import timedelta
from math import pi, sin
from struct import pack

import pytest
from app import call_records as recording
from app.calling import audio_tracks
from app.platform.db import MODELS
from journey import answered_by_human, call, get, hang_up, import_leads, post, reserve, run_jobs, wrap_up
from sqlalchemy import select


@pytest.mark.parametrize(
    ("recipient", "reason", "sure"),
    [
        ({"phone": "+12125550100"}, "one_party", True),  # New York
        ({"phone": "+15125550100"}, "one_party", True),  # Texas
        ({"phone": "+12025550100"}, "one_party", True),  # District of Columbia
        ({"phone": "+14155550100"}, "CA", False),
        ({"phone": "+12035550100"}, "CT", False),
        ({"phone": "+12125550100", "state": "FL"}, "FL", False),  # a supplied stricter state wins
        ({"phone": "+14155550100", "state": "NY"}, "CA", False),  # so does a stricter area code
        ({"phone": "+18005550100"}, "toll_free", False),
        ({"phone": "+18005550100", "state": "NY"}, "toll_free", False),
        ({"phone": "+14165550100"}, "outside_us", False),
    ],
)
def test_the_prospect_is_kept_only_when_sure(recipient, reason, sure):
    rules = recording.basis(recipient)
    assert (rules["reason"], rules["prospect"]) == (reason, sure)
    assert rules["rep"] is True and rules["prospect_live"] is True


def test_the_stricter_list_and_switches_change_the_rules():
    assert recording.basis({"phone": "+14155550100"}, states=("CT",))["prospect"] is True
    held = recording.basis({"phone": "+14155550100"}, transcribe_prospect_stricter=False)
    assert held["prospect_live"] is False
    assert recording.side_policy({"recording": held}, "remote", live=True) == (False, False)
    consented = {"recording": {**held, "consent_at": "2026-11-28T10:00:00+00:00"}}
    assert recording.side_policy(consented, "remote", live=True) == (True, True)
    assert recording.basis({"phone": "+12125550100"}, record_rep=False)["rep"] is False
    assert recording.side_policy({"recording": held}, "rep", live=False) == (False, True)


def place(ws, name, phone, **row):
    import_leads(ws, [{"name": name, "phone": phone, **row}], list_name=name)
    reservation = reserve(ws, list_names=[name]).json()
    placed = post(
        ws, "/v1/calls/start", {"reservation_id": reservation["reservation_id"], "idempotency_key": name}
    )
    answered_by_human(ws, placed["id"])
    return placed["id"]


def say(ws, call_id, segment_id, text, side, start_ms):
    return post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {
            "segment_id": segment_id,
            "revision": 1,
            "side": side,
            "text": text,
            "is_final": True,
            "start_ms": start_ms,
            "end_ms": start_ms + 2000,
        },
    )


def end(ws, call_id):
    hang_up(ws, call_id)
    wrap_up(ws, call_id, outcome="not_interested")
    run_jobs(ws)


def kept(ws, call_id):
    return {(row["side"], row["text"]) for row in get(ws, f"/v1/calls/{call_id}/recording")["transcript"]}


REP = "Hi, this is Sam with Example Seller, do you have a minute?"
THEM = "Sure, go ahead, I have a minute right now."


def test_a_stricter_state_call_keeps_only_the_reps_side(workspace):
    call_id = place(workspace, "California lead", "+14155550123")
    live = get(workspace, f"/v1/calls/{call_id}")
    assert live["recording"]["reason"] == "CA" and live["recording"]["prospect"] is False
    say(workspace, call_id, "r1", REP, "rep", 0)
    say(workspace, call_id, "s1", THEM, "remote", 3000)
    assert {row["text"] for row in get(workspace, f"/v1/calls/{call_id}")["transcript"]} == {REP, THEM}, (
        "Both sides feed the live helper during the call"
    )
    end(workspace, call_id)
    assert kept(workspace, call_id) == {("rep", REP)}
    late = say(workspace, call_id, "s2", "Call me back next quarter.", "remote", 9000)
    assert late.get("unavailable") == "call_ended"


def test_a_one_party_state_call_keeps_both_sides(workspace):
    call_id = place(workspace, "New York lead", "+12125550123")
    say(workspace, call_id, "r1", REP, "rep", 0)
    say(workspace, call_id, "s1", THEM, "remote", 3000)
    end(workspace, call_id)
    assert kept(workspace, call_id) == {("rep", REP), ("remote", THEM)}
    detail = get(workspace, f"/v1/calls/{call_id}/recording")["recording"]
    assert detail["reason"] == "one_party" and detail["status"] == "no_audio"


def test_pressing_r_keeps_the_prospects_side_from_that_moment(workspace, clock):
    call_id = place(workspace, "Florida consent", "+13055550123")
    say(workspace, call_id, "s0", "Who is this, and what is this about?", "remote", 0)
    clock.now += timedelta(seconds=2)
    rules = post(workspace, f"/v1/calls/{call_id}/recording/consent")["recording"]
    assert rules["consent_at"] is not None
    clock.now += timedelta(seconds=2)
    say(workspace, call_id, "s1", THEM, "remote", 5000)
    end(workspace, call_id)
    assert kept(workspace, call_id) == {("remote", THEM)}


def test_the_rep_can_stop_keeping_their_own_side(workspace):
    call(workspace, "PUT", "/v1/admin/settings", json={"record_rep": False})
    call_id = place(workspace, "Texas lead", "+15125550123")
    say(workspace, call_id, "r1", REP, "rep", 0)
    say(workspace, call_id, "s1", THEM, "remote", 3000)
    end(workspace, call_id)
    assert kept(workspace, call_id) == {("remote", THEM)}


def test_settings_show_and_change_the_recording_rules(workspace):
    saved = call(
        workspace,
        "PUT",
        "/v1/admin/settings",
        json={
            "transcribe_prospect_stricter": False,
            "recording_states": ["CA", "WA"],
            "recording_retention_months": 6,
        },
    )
    assert saved["recording"]["transcribe_prospect_stricter"] is False
    assert saved["recording"]["recording_states"] == ["CA", "WA"]
    assert saved["recording"]["recording_retention_months"] == 6
    assert "FL" in saved["recording"]["stricter_default"]
    call(workspace, "PUT", "/v1/admin/settings", status=422, json={"recording_states": ["California"]})
    call_id = place(workspace, "Florida not stricter", "+13055550124")
    assert get(workspace, f"/v1/calls/{call_id}")["recording"]["prospect"] is True


def test_an_opt_out_deletes_that_phones_recordings(workspace):
    call_id = place(workspace, "New York opt out", "+12125550124")
    say(workspace, call_id, "r1", REP, "rep", 0)
    say(workspace, call_id, "s1", THEM, "remote", 3000)
    end(workspace, call_id)
    assert kept(workspace, call_id)
    post(workspace, "/v1/suppressions", {"phone": "+12125550124", "reason": "opt_out"})
    assert kept(workspace, call_id) == set()
    assert get(workspace, f"/v1/calls/{call_id}/recording")["recording"]["status"] == "opt_out"


def test_one_call_or_every_call_can_be_deleted(workspace):
    first = place(workspace, "Delete one", "+12125550125")
    say(workspace, first, "r1", REP, "rep", 0)
    end(workspace, first)
    second = place(workspace, "Delete all", "+12125550126")
    say(workspace, second, "r1", REP, "rep", 0)
    end(workspace, second)
    assert call(workspace, "DELETE", f"/v1/calls/{first}/recording") == {"deleted": True}
    assert kept(workspace, first) == set() and kept(workspace, second)
    assert call(workspace, "DELETE", "/v1/recordings")["deleted"] >= 1
    assert kept(workspace, second) == set()


def stored(app, call_id):
    service = app.state.service
    with service.db.transaction(False) as s:
        row = s.get(MODELS["call_attempts"], call_id)
        model = MODELS["transcript_segments"]
        texts = [r.data.get("text") for r in s.scalars(select(model).where(model.ctx_key == call_id))]
        return row.data["recording"], [text for text in texts if text]


def test_auto_delete_removes_recordings_older_than_the_chosen_months(workspace, app, clock):
    call(workspace, "PUT", "/v1/admin/settings", json={"recording_retention_months": 1})
    call_id = place(workspace, "Old call", "+12125550127")
    say(workspace, call_id, "r1", REP, "rep", 0)
    end(workspace, call_id)
    clock.now += timedelta(days=29)
    recording.purge_expired(app.state.service)
    assert stored(app, call_id)[1] == [REP]
    clock.now += timedelta(days=2)
    recording.purge_expired(app.state.service)
    rules, texts = stored(app, call_id)
    assert texts == [] and rules["status"] == "expired"


def tone(frequency, frames):
    one = pack("<1600h", *(int(6000 * sin(2 * pi * frequency * i / 16000)) for i in range(1600)))
    return [one] * frames


def test_kept_audio_is_one_encrypted_stereo_file_aligned_with_the_transcript(workspace, app):
    import soundfile

    service = app.state.service
    call_id = place(workspace, "New York audio", "+12125550128")
    tenant = workspace["tenant_id"]
    audio, rep = audio_tracks.open_side(tenant, call_id, "rep", recording.folder(service), now=100.0)
    _, remote = audio_tracks.open_side(tenant, call_id, "remote", recording.folder(service), now=100.5)
    for side, frames in ((rep, tone(220, 20)), (remote, tone(440, 10))):
        for frame in frames:
            audio_tracks.write_part(audio, side, audio_tracks.accept(audio, side, frame, keep=True))
        side.closed = True
    end(workspace, call_id)
    detail = get(workspace, f"/v1/calls/{call_id}/recording")["recording"]
    assert detail["status"] == "saved" and detail["format"] == "ogg_opus"
    assert detail["tracks"] == {"rep": {"start_ms": 0}, "remote": {"start_ms": 500}}
    assert 1900 <= detail["duration_ms"] <= 2100
    stored = (recording.folder(service) / f"{call_id}.ogg.enc").read_bytes()
    assert not stored.startswith(b"OggS"), "Audio at rest is encrypted"
    assert not list(recording.folder(service).glob("*.pcm")), "No raw audio is left behind"
    response = workspace["client"].get(f"/v1/calls/{call_id}/recording/audio", headers=workspace["headers"])
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    samples, rate = soundfile.read(io.BytesIO(response.content), dtype="int16")
    assert rate == 16000 and samples.shape[1] == 2
    assert abs(samples[:7000, 1]).max() < 500, "The prospect channel is silent before their stream began"
    assert abs(samples[9000:15000, 1]).max() > 2000
    assert abs(samples[:15000, 0]).max() > 2000


def test_a_stricter_state_call_saves_only_the_reps_channel(workspace, app):
    service = app.state.service
    call_id = place(workspace, "California audio", "+14155550129")
    tenant = workspace["tenant_id"]
    audio, rep = audio_tracks.open_side(tenant, call_id, "rep", recording.folder(service), now=10.0)
    _, remote = audio_tracks.open_side(tenant, call_id, "remote", recording.folder(service), now=10.0)
    rules = get(workspace, f"/v1/calls/{call_id}")["recording"]
    for side, name in ((rep, "rep"), (remote, "remote")):
        _, keep = recording.side_policy({"recording": rules}, name, live=True)
        for frame in tone(300, 10):
            audio_tracks.write_part(audio, side, audio_tracks.accept(audio, side, frame, keep=keep))
        side.closed = True
    end(workspace, call_id)
    detail = get(workspace, f"/v1/calls/{call_id}/recording")["recording"]
    assert detail["status"] == "saved" and list(detail["tracks"]) == ["rep"]


def test_no_kept_text_reaches_events_jobs_or_the_crm(workspace, app):
    call_id = place(workspace, "New York private", "+12125550130")
    marker = "Our renewal is with Initech in March."
    say(workspace, call_id, "s1", marker, "remote", 0)
    end(workspace, call_id)
    assert ("remote", marker) in kept(workspace, call_id)
    service = app.state.service
    with service.db.transaction(False) as s:
        from app.platform.db import Job

        assert marker not in str([job.payload for job in s.scalars(select(Job))])
        assert marker not in str([row.data for row in s.scalars(select(MODELS["webhook_inbox"]))])
    assert marker not in str(get(workspace, "/v1/events"))

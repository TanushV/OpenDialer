"""Call insights plan phase 6: per-call numbers, from who spoke when and the rep's own voice. No voiceprints (C5)."""

import math
from struct import pack

import pytest
from app import call_metrics, call_records
from app.calling import audio_tracks
from app.platform.db import MODELS
from test_call_recording_rules import end, place, say


def test_timing_numbers_from_each_sides_speech():
    rep = [[0, 4000], [6300, 15000], [16000, 18000]]
    prospect = [[4500, 6000], [10000, 11000], [15200, 15800]]
    numbers = call_metrics.timing(rep, prospect, [])
    assert numbers["rep_talk_ms"] == 4000 + 8700 + 2000 and numbers["prospect_talk_ms"] == 1500 + 1000 + 600
    assert numbers["talk_share"] == round(14700 / (14700 + 3100), 3)
    assert numbers["longest_rep_monologue_ms"] == 8700
    assert numbers["median_response_ms"] == 300, "The rep answered 300 ms after the prospect stopped"
    assert numbers["prospect_interruptions"] == 1, "The prospect cut in at 10 s, mid-monologue"
    assert numbers["rep_interruptions"] == 0
    assert numbers["duration_ms"] == 18000


def test_script_parts_words_and_questions_come_from_segment_metadata():
    segments = [
        {
            "side": "rep",
            "is_final": True,
            "start_ms": 0,
            "end_ms": 3000,
            "script_section": "opening:opening-1",
            "word_count": 9,
            "question_count": 0,
        },
        {"side": "remote", "is_final": True, "start_ms": 3500, "end_ms": 5000, "word_count": 4},
        {
            "side": "rep",
            "is_final": True,
            "start_ms": 5500,
            "end_ms": 12500,
            "script_section": "pitch:pitch-1",
            "word_count": 21,
            "question_count": 1,
        },
        {
            "side": "rep",
            "is_final": True,
            "start_ms": 13000,
            "end_ms": 15000,
            "bleed": True,
            "word_count": 50,
        },
    ]
    numbers = call_metrics.compute(segments)
    assert numbers["source"] == "transcript"
    assert numbers["time_to_pitch_ms"] == 5500
    assert numbers["part_ms"] == {"opening": 3000, "pitch": 7000}
    assert numbers["rep_questions"] == 1
    assert numbers["rep_words_per_minute"] == round(30 / (10000 / 60000)), "Bleed is not the rep's speech"


def glide(start_hz, end_hz, seconds, rate=16000):
    samples, phase = [], 0.0
    count = int(rate * seconds)
    for i in range(count):
        frequency = start_hz + (end_hz - start_hz) * i / count
        phase += 2 * math.pi * frequency / rate
        samples.append(int(7000 * math.sin(phase)))
    return pack(f"<{count}h", *samples)


def test_the_reps_pitch_range_and_loudness_from_their_own_audio():
    sound = call_metrics.voice(glide(120, 220, 3.0))
    assert 8 <= sound["pitch_range_semitones"] <= 11, sound
    assert -20 < sound["loudness_dbfs"] < -5
    flat = call_metrics.voice(glide(150, 150, 3.0))
    assert flat["pitch_range_semitones"] < 1, "A monotone voice has almost no range"


def stored_metrics(app, call_id):
    with app.state.db.transaction(False) as s:
        return s.get(MODELS["call_attempts"], call_id).data.get("metrics")


@pytest.mark.parametrize(("phone", "prospect_tone"), [("+12125550140", True), ("+14155550141", False)])
def test_a_finished_call_gets_its_numbers_and_prospect_tone_only_where_kept(
    workspace, app, phone, prospect_tone
):
    service = app.state.service
    call_id = place(workspace, f"Metrics {phone}", phone)
    rules = call_records.basis({"phone": phone})
    audio, rep = audio_tracks.open_side(
        workspace["tenant_id"], call_id, "rep", call_records.folder(service), now=1.0
    )
    _, remote = audio_tracks.open_side(
        workspace["tenant_id"], call_id, "remote", call_records.folder(service), now=1.0
    )
    voiced, quiet = glide(120, 220, 2.0), bytes(32000)
    sequence = [(rep, voiced), (remote, quiet), (rep, quiet), (remote, glide(180, 260, 2.0))]
    for side, pcm in sequence:
        other = remote if side is rep else rep
        keep = call_records.side_policy({"recording": rules}, side.name, live=True)[1]
        for chunk in range(0, len(pcm), 3200):
            frame = pcm[chunk : chunk + 3200]
            audio_tracks.write_part(audio, side, audio_tracks.accept(audio, side, frame, keep=keep))
            other_keep = call_records.side_policy({"recording": rules}, other.name, live=True)[1]
            audio_tracks.write_part(
                audio, other, audio_tracks.accept(audio, other, bytes(3200), keep=other_keep)
            )
    rep.closed = remote.closed = True
    say(workspace, call_id, "r1", "Hi, this is Sam with Example Seller, do you have a minute?", "rep", 0)
    end(workspace, call_id)
    numbers = stored_metrics(app, call_id)
    assert numbers["source"] == "audio"
    assert numbers["rep_talk_ms"] > 1500 and numbers["prospect_talk_ms"] > 1500
    assert numbers["rep_pitch_range_semitones"] > 5
    assert ("prospect_pitch_range_semitones" in numbers) is prospect_tone


def test_stored_numbers_are_only_the_named_list_and_never_a_voice_vector(workspace, app):
    call_id = place(workspace, "Named numbers", "+12125550142")
    say(workspace, call_id, "r1", "Hi, this is Sam with Example Seller, do you have a minute?", "rep", 0)
    say(workspace, call_id, "s1", "Sure, go ahead, I have a minute right now.", "remote", 3000)
    end(workspace, call_id)
    numbers = stored_metrics(app, call_id)
    assert set(numbers) <= call_metrics.ALLOWED
    for key, value in numbers.items():
        if key == "part_ms":
            assert all(isinstance(v, int) for v in value.values())
        elif key != "source":
            assert value is None or isinstance(value, int | float), (key, value)


def test_older_calls_get_their_numbers_once(workspace, app):
    call_id = place(workspace, "Older call", "+12125550143")
    say(workspace, call_id, "r1", "Hi, this is Sam with Example Seller, do you have a minute?", "rep", 0)
    end(workspace, call_id)
    with app.state.db.transaction() as s:
        row = s.get(MODELS["call_attempts"], call_id)
        row.data = {k: v for k, v in row.data.items() if k != "metrics"}
    assert call_records.backfill_metrics(app.state.service) >= 1
    assert stored_metrics(app, call_id)["source"] == "transcript"

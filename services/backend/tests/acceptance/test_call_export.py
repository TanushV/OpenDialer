"""Call insights plan phase 7: calls, audio and transcripts export as one bundle for analysis anywhere."""

import csv
import io
import json
import zipfile
from pathlib import Path

from app import call_records
from app.calling import audio_tracks
from journey import post
from test_call_recording_rules import REP, THEM, end, place, say, tone


def with_audio(workspace, app, call_id):
    service = app.state.service
    audio, rep = audio_tracks.open_side(
        workspace["tenant_id"], call_id, "rep", call_records.folder(service), now=5.0
    )
    _, remote = audio_tracks.open_side(
        workspace["tenant_id"], call_id, "remote", call_records.folder(service), now=5.0
    )
    rules = call_records.basis({"phone": "+12125550150"})
    for side in (rep, remote):
        keep = call_records.side_policy({"recording": rules}, side.name, live=True)[1]
        for frame in tone(250 if side is rep else 400, 15):
            audio_tracks.write_part(audio, side, audio_tracks.accept(audio, side, frame, keep=keep))
        side.closed = True


def bundle(result):
    return zipfile.ZipFile(io.BytesIO(Path(result["path"]).read_bytes()))


def rows(archive, name="calls.csv"):
    return list(csv.DictReader(io.StringIO(archive.read(name).decode())))


def test_the_bundle_pairs_each_calls_audio_with_its_kept_transcript(workspace, app):
    import soundfile

    kept = place(workspace, "Export New York", "+12125550150")
    with_audio(workspace, app, kept)
    say(workspace, kept, "r1", REP, "rep", 0)
    say(workspace, kept, "s1", THEM, "remote", 3000)
    end(workspace, kept)
    rep_only = place(workspace, "Export California", "+14155550151")
    say(workspace, rep_only, "r1", REP, "rep", 0)
    say(workspace, rep_only, "s1", THEM, "remote", 3000)
    end(workspace, rep_only)
    result = post(workspace, "/v1/review/export", {})
    assert result["calls"] >= 2 and result["audio"] == 1 and result["transcripts"] >= 2
    assert Path(result["path"]).parent.name == "Exports"
    with bundle(result) as archive:
        names = set(archive.namelist())
        assert {
            "README.txt",
            "calls.csv",
            "script-events.csv",
            "script-structure.csv",
            "stage-statistics.csv",
        } <= names
        table = {row["call_id"]: row for row in rows(archive)}
        both, mine = table[kept], table[rep_only]
        assert (both["recording_reason"], both["prospect_side_kept"]) == ("one_party", "True")
        assert (mine["recording_reason"], mine["prospect_side_kept"]) == ("CA", "False")
        assert both["audio_file"] == f"calls/{kept}/audio.ogg" and not mine["audio_file"]
        assert both["metrics_source"] == "audio" and both["rep_talk_ms"]
        assert both["lead_name"] == "Export New York" and both["phone"] == "+12125550150"
        audio = archive.read(both["audio_file"])
        assert audio.startswith(b"OggS"), "Exported audio is the plain Ogg Opus, ready for other tools"
        samples, rate = soundfile.read(io.BytesIO(audio), dtype="int16")
        assert rate == 16000 and samples.shape[1] == 2
        lines = [
            json.loads(line) for line in archive.read(f"calls/{kept}/transcript.jsonl").decode().splitlines()
        ]
        assert [(line["side"], line["text"]) for line in lines] == [("rep", REP), ("remote", THEM)]
        assert lines[1]["start_ms"] == 3000
        only = [
            json.loads(line)
            for line in archive.read(f"calls/{rep_only}/transcript.jsonl").decode().splitlines()
        ]
        assert [line["side"] for line in only] == ["rep"], "The prospect's words were not kept in California"
        assert "Prospect:" in archive.read(f"calls/{kept}/transcript.txt").decode()
        assert "personal data" in archive.read("README.txt").decode()


def test_filters_and_audio_choice_narrow_the_bundle(workspace, app):
    first = place(workspace, "Filter one", "+12125550152")
    with_audio(workspace, app, first)
    say(workspace, first, "r1", REP, "rep", 0)
    end(workspace, first)
    result = post(workspace, "/v1/review/export", {"list_name": "Filter one", "include_audio": False})
    with bundle(result) as archive:
        assert [row["call_id"] for row in rows(archive)] == [first]
        assert not [name for name in archive.namelist() if name.endswith(".ogg")]
    none = post(workspace, "/v1/review/export", {"variant": "nothing-like-this"})
    assert none["calls"] == 0
    talks = post(workspace, "/v1/review/export", {"conversations_only": True})
    assert talks["calls"] >= 1, "Not interested is a conversation"
    assert post(workspace, "/v1/review/export", {"outcome": "voicemail"})["calls"] == 0

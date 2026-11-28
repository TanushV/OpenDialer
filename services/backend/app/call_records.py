"""What a call keeps: the rep's side always, the prospect's side only when sure it is allowed (call insights plan P1).

Sure means one-party consent applies to the prospect: their supplied state (if any) and their area code's state are
both known and both off the stricter list. Toll-free, unknown and non-US numbers are never sure. On any other call the
rep can say the call is recorded and press R, which keeps the prospect's side from that moment. Live transcription for
the helper is separate (P2): it runs on every call unless the "Transcribe the prospect live in stricter states" switch
is off. Audio is recorded on this computer only (P3) and kept until deleted, with optional auto-delete (P4).
"""

import asyncio
import io
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app import call_metrics, ops
from app.calling import audio_tracks
from app.optimization.geography import AREA_STATES, normalize_state, us_area
from app.platform.db import MODELS, emit, update, workspace

log = logging.getLogger(__name__)

# The broad list of states that need every party's consent; editable in Settings (Advanced).
STRICTER = ("CA", "CT", "DE", "FL", "IL", "MD", "MA", "MI", "MT", "NV", "NH", "PA", "WA")
KEPT_UNTIL = datetime(9999, 1, 1, tzinfo=UTC)
FORMAT = "ogg_opus"
SAMPLE_RATE = 16000


def settings(service, p):
    config = service.configs.read(workspace(p))
    states = config.get("recording_states")
    return {
        "record_rep": config.get("record_rep", True) is not False,
        "transcribe_prospect_stricter": config.get("transcribe_prospect_stricter", True) is not False,
        "recording_states": tuple(states) if isinstance(states, list) else STRICTER,
        "recording_retention_months": config.get("recording_retention_months"),
    }


def basis(recipient, *, record_rep=True, transcribe_prospect_stricter=True, states=STRICTER):
    """What this call may keep and transcribe, from the prospect's phone and supplied state."""
    phone = str(recipient.get("phone") or "")
    area = us_area(phone)
    supplied = normalize_state(recipient.get("state")) or normalize_state(recipient.get("jurisdiction"))
    by_code = AREA_STATES.get(phone[2:5]) if area == "state" else None
    known = [state for state in (supplied, by_code) if state]
    stricter = next((state for state in known if state in states), None)
    if area is None:
        reason = "outside_us"
    elif area == "toll_free":
        reason = stricter or "toll_free"
    elif stricter:
        reason = stricter
    elif not by_code:
        reason = "unknown"
    else:
        reason = "one_party"
    sure = reason == "one_party"
    return {
        "rep": bool(record_rep),
        "prospect": sure,
        "prospect_live": sure or bool(transcribe_prospect_stricter),
        "reason": reason,
        "consent_at": None,
        "consent_ms": None,
        "status": "pending",
    }


def basis_for(service, s, p, call):
    recipient, _ = ops.call_party(service, s, p, call)
    chosen = settings(service, p)
    return basis(
        {**recipient.data, "phone": recipient.data.get("phone")},
        record_rep=chosen["record_rep"],
        transcribe_prospect_stricter=chosen["transcribe_prospect_stricter"],
        states=chosen["recording_states"],
    )


def side_policy(call_data, side, *, live):
    """(transcribe live, keep audio) for one side right now."""
    rules = call_data.get("recording") or {}
    if side == "rep":
        return live, bool(rules.get("rep"))
    consented = rules.get("consent_at") is not None
    return live and bool(rules.get("prospect_live") or consented), bool(rules.get("prospect") or consented)


def folder(service):
    return Path(service.settings.data_dir) / "recordings"


def keeps_text(rules, row):
    """Whether a finished call keeps this segment's text under P1 (bleed is never the rep's speech)."""
    if not row.get("is_final") or row.get("system_audio"):
        return False
    if row.get("side") == "rep":
        return bool(rules.get("rep")) and not row.get("bleed")
    if row.get("side") != "remote":
        return False
    if rules.get("prospect"):
        return True
    if rules.get("consent_ms") is not None and row.get("start_ms") is not None:
        return row["start_ms"] >= rules["consent_ms"]
    if rules.get("consent_at") and row.get("server_received_at"):
        return row["server_received_at"] >= rules["consent_at"]
    return False


def keep_transcript(service, s, p, call):
    """At call end: keep the text P1 allows (until deleted), delete the rest."""
    rules = call.data.get("recording") or {}
    model = MODELS["transcript_segments"]
    kept = 0
    for row in s.scalars(select(model).where(model.tenant_id == p["tenant_id"], model.ctx_key == call.id)):
        if "text" not in row.data:
            continue
        if rules and keeps_text(rules, row.data):
            row.ctx_expires_at = KEPT_UNTIL
            update(row, kept=True)
            kept += 1
        else:
            row.data = {k: v for k, v in row.data.items() if k != "text"}
    service.raw.revoke_scope(p["tenant_id"] + ":" + call.id)
    return kept


def enqueue_finalize(service, s, p, call):
    if not call.data.get("recording"):
        return
    service.jobs.enqueue_in(
        s,
        "postcall",
        {
            "kind": "recording_finalize",
            "call_id": call.id,
            "seller_id": p["seller_id"],
            "user_id": call.data.get("rep_id"),
        },
        f"recording-finalize:{call.id}",
        tenant_id=p["tenant_id"],
        priority=50,
        available_at=service.clock(),
    )


def _read_part(path, kept_start_ms):
    if not path.exists():
        return None
    data = path.read_bytes()
    return (kept_start_ms or 0, data) if data else None


def encode(tracks):
    """One stereo Ogg Opus file from {side: (start_ms, pcm)}: rep left, prospect right, silence where not kept."""
    import numpy as np
    import soundfile

    channels = []
    end = max(start + len(pcm) // audio_tracks.BYTES_PER_MS for start, pcm in tracks.values())
    length = end * SAMPLE_RATE // 1000
    for side in ("rep", "remote"):
        channel = np.zeros(length, dtype=np.int16)
        if side in tracks:
            start, pcm = tracks[side]
            samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
            first = start * SAMPLE_RATE // 1000
            channel[first : first + len(samples)] = samples[: max(0, length - first)]
        channels.append(channel)
    stereo = np.stack(channels, axis=1)
    buffer = io.BytesIO()
    soundfile.write(buffer, stereo, SAMPLE_RATE, format="OGG", subtype="OPUS", compression_level=0.95)
    return buffer.getvalue(), round(length * 1000 / SAMPLE_RATE)


async def finalize(service, p, call_id, *, wait_seconds=5.0):
    """Turn a finished call's kept audio into one encrypted file, once its sockets have let go."""
    deadline = asyncio.get_running_loop().time() + wait_seconds
    audio = audio_tracks.current(p["tenant_id"], call_id)
    while audio and not all(side.closed for side in audio.sides.values()):
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.1)
    await asyncio.to_thread(_finalize, service, p, call_id)


def _finalize(service, p, call_id):
    from app.platform.secret_files import cipher

    with service.db.transaction(False) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        rules = dict(call.data.get("recording") or {})
        revoked = bool(call.data.get("transcription_disabled"))
    audio = audio_tracks.forget(p["tenant_id"], call_id)
    base = folder(service)
    if not rules or rules.get("status") not in {"pending", "capturing"} or revoked:
        # Deleted, opted out or already saved: nothing of this call's raw audio stays on disk.
        for side in ("rep", "remote"):
            (base / f"{call_id}.{side}.pcm").unlink(missing_ok=True)
        if revoked and rules.get("status") in {"pending", "capturing"}:
            with service.db.transaction() as s:
                call = ops.required(service, s, "call_attempts", call_id, p)
                update(call, recording={**rules, "status": "opt_out", "file": None, "bytes": 0})
        return
    starts = dict(rules.get("tracks") or {})
    if audio:
        for side in audio.sides.values():
            audio_tracks.write_part(audio, side, audio_tracks.drain(audio, side))
            if side.kept_start_ms is not None:
                starts[side.name] = side.kept_start_ms
    tracks = {}
    for side in ("rep", "remote"):
        part = _read_part(base / f"{call_id}.{side}.pcm", starts.get(side))
        if part:
            tracks[side] = part
    changes = {"tracks": {side: {"start_ms": start} for side, (start, _) in tracks.items()}}
    if audio:
        changes["activity"] = {side: audio_tracks.activity(audio, side) for side in audio.sides}
    if tracks:
        try:
            encoded, duration = encode(tracks)
            name = f"{call_id}.ogg.enc"
            target = base / name
            target.write_bytes(cipher().encrypt(encoded))
            changes.update(status="saved", file=name, format=FORMAT, bytes=len(encoded), duration_ms=duration)
        except Exception as exc:  # The transcript and metadata stand; the audio could not be saved.
            log.warning("Call %s recording could not be encoded: %s", call_id, type(exc).__name__)
            changes.update(status="failed", error=type(exc).__name__)
    else:
        changes.update(status="no_audio")
    with service.db.transaction(False) as s:
        segments = [row.data for row in ops.transcript_metadata(service, s, p, call_id)]
    try:
        metrics = call_metrics.compute(
            segments,
            changes.get("activity"),
            rep_pcm=tracks["rep"][1] if "rep" in tracks else None,
            prospect_pcm=tracks["remote"][1] if "remote" in tracks else None,
        )
    except Exception as exc:  # The recording stands without its numbers.
        log.warning("Call %s metrics could not be computed: %s", call_id, type(exc).__name__)
        metrics = None
    for side in ("rep", "remote"):
        (base / f"{call_id}.{side}.pcm").unlink(missing_ok=True)
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        update(call, recording={**(call.data.get("recording") or {}), **changes})
        if metrics is not None:
            update(call, metrics=metrics)
        emit(s, p["tenant_id"], "call.updated", call, service.clock())


def backfill_metrics(service, limit=200):
    """Finished calls from before the call numbers get them once, from their segment timings (phase 6)."""
    from app.calling.state import TERMINAL

    model = MODELS["call_attempts"]
    with service.db.transaction(False) as s:
        due = [
            (call.id, {"tenant_id": call.tenant_id, "seller_id": call.seller_id})
            for call in s.scalars(
                select(model).where(model.op_status.in_(TERMINAL)).order_by(model.op_at.desc())
            )
            if "metrics" not in call.data and call.data.get("answered_at")
        ][:limit]
    for call_id, p in due:
        with service.db.transaction() as s:
            segments = [row.data for row in ops.transcript_metadata(service, s, p, call_id)]
            call = ops.required(service, s, "call_attempts", call_id, p)
            update(call, metrics=call_metrics.compute(segments))
    return len(due)


def note_capture(service, s, p, call, side, start_ms):
    rules = dict(call.data.get("recording") or {})
    if rules and rules.get("status") == "pending":
        rules["status"] = "capturing"
    tracks = dict(rules.get("tracks") or {})
    if side not in tracks:
        tracks[side] = {"start_ms": start_ms}
        rules["tracks"] = tracks
        update(call, recording=rules)


def consent(service, p, call_id):
    """The rep said the call is recorded: keep (and, if held, transcribe) the prospect's side from now."""
    from app.calling.state import TERMINAL
    from app.platform.errors import fail

    audio = audio_tracks.current(p["tenant_id"], call_id)
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        if call.data.get("rep_id") != p["user_id"]:
            fail(403, "Only the rep on the call can record consent")
        if call.data["state"] in TERMINAL | {"ENDING"}:
            fail(409, "The call has ended")
        rules = dict(call.data.get("recording") or {})
        if not rules:
            fail(409, "This call has not been answered")
        if not rules.get("prospect") and rules.get("consent_at") is None:
            position = None
            if audio and audio.sides:
                position = max(side.position_ms(audio.offset_ms(side)) for side in audio.sides.values())
            rules.update(consent_at=service.clock().isoformat(), consent_ms=position)
            update(call, recording=rules)
            emit(s, p["tenant_id"], "call.updated", call, service.clock())
        return rules


def _delete_files(service, call_id):
    base = folder(service)
    for name in (f"{call_id}.ogg.enc", f"{call_id}.rep.pcm", f"{call_id}.remote.pcm"):
        (base / name).unlink(missing_ok=True)


def forget_call(service, s, p, call, reason="deleted"):
    """Delete a call's audio and transcript text; its word-free metadata stays for statistics."""
    model = MODELS["transcript_segments"]
    for row in s.scalars(select(model).where(model.tenant_id == p["tenant_id"], model.ctx_key == call.id)):
        if "text" in row.data:
            row.data = {k: v for k, v in row.data.items() if k not in {"text", "kept"}}
    rules = dict(call.data.get("recording") or {})
    if rules:
        rules.update(status=reason, file=None, bytes=0, deleted_at=service.clock().isoformat())
        update(call, recording=rules)
    service.raw.revoke_scope(p["tenant_id"] + ":" + call.id)
    call_id = call.id
    s.info.setdefault("after_transaction", []).append(lambda: _delete_files(service, call_id))


def forget_endpoint(service, s, p, endpoint_id):
    """An opt-out deletes every recording of that phone (P4)."""
    model = MODELS["call_attempts"]
    for call in s.scalars(
        select(model).where(
            model.tenant_id == p["tenant_id"],
            model.seller_id == p["seller_id"],
            model.op_endpoint_id == endpoint_id,
        )
    ):
        if call.data.get("recording"):
            forget_call(service, s, p, call, reason="opt_out")


def forget_all(service, p):
    model = MODELS["call_attempts"]
    count = 0
    with service.db.transaction() as s:
        for call in s.scalars(
            select(model).where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
        ):
            if (call.data.get("recording") or {}).get("status") not in {
                None,
                "deleted",
                "opt_out",
                "expired",
            }:
                forget_call(service, s, p, call)
                count += 1
    return count


def purge_expired(service):
    """Auto-delete (P4, off by default): recordings of calls older than the chosen number of months."""
    from app.platform.errors import dt

    model = MODELS["call_attempts"]
    tenants = set()
    with service.db.transaction(False) as s:
        tenants = {row for row in s.scalars(select(MODELS["call_attempts"].tenant_id).distinct())}
    for tenant in tenants:
        months = service.configs.read(tenant).get("recording_retention_months")
        if not months:
            continue
        cutoff = service.clock() - timedelta(days=30 * int(months))
        with service.db.transaction() as s:
            for call in s.scalars(select(model).where(model.tenant_id == tenant, model.op_at < cutoff)):
                rules = call.data.get("recording") or {}
                started = dt(call.data.get("started_at"))
                if rules.get("status") in {"saved", "no_audio", "failed"} and started and started < cutoff:
                    p = {"tenant_id": tenant, "seller_id": call.seller_id}
                    forget_call(service, s, p, call, reason="expired")


def sweep_unfinished(service):
    """After a restart: finish recordings whose sockets are gone and whose call ended a minute ago."""
    from app.calling.state import TERMINAL
    from app.platform.errors import dt

    model = MODELS["call_attempts"]
    due = []
    recent = service.clock() - timedelta(days=2)
    with service.db.transaction(False) as s:
        query = select(model).where(model.op_status.in_(TERMINAL), model.op_at >= recent).limit(500)
        for call in s.scalars(query):
            rules = call.data.get("recording") or {}
            ended = dt(call.data.get("ended_at"))
            if (
                rules.get("status") in {"pending", "capturing"}
                and ended
                and (service.clock() - ended).total_seconds() > 60
                and not audio_tracks.current(call.tenant_id, call.id)
            ):
                due.append(({"tenant_id": call.tenant_id, "seller_id": call.seller_id}, call.id))
    for p, call_id in due:
        _finalize(service, p, call_id)


def disk_bytes(service):
    base = folder(service)
    return sum(path.stat().st_size for path in base.glob("*")) if base.exists() else 0


def audio_wav(service, p, call_id):
    """Decrypted audio as WAV for playback in the app (WebKit plays WAV everywhere)."""
    import soundfile

    from app.platform.errors import fail
    from app.platform.secret_files import cipher

    with service.db.transaction(False) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        rules = call.data.get("recording") or {}
    if rules.get("status") != "saved" or not rules.get("file"):
        fail(404, "No saved audio for this call")
    data = cipher().decrypt((folder(service) / rules["file"]).read_bytes())
    samples, rate = soundfile.read(io.BytesIO(data), dtype="int16")
    out = io.BytesIO()
    soundfile.write(out, samples, rate, format="WAV", subtype="PCM_16")
    return out.getvalue()


def audio_ogg(service, call_data):
    from app.platform.secret_files import cipher

    rules = call_data.get("recording") or {}
    if rules.get("status") != "saved" or not rules.get("file"):
        return None
    path = folder(service) / rules["file"]
    return cipher().decrypt(path.read_bytes()) if path.exists() else None

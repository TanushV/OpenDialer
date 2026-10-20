from hashlib import sha256

# Telnyx `inbound` is the audio of the leg that started the stream; `outbound` is the other leg.
LEG_SIDES = {
    "prospect": {"inbound": "remote", "outbound": "rep"},
    "agent": {"inbound": "rep", "outbound": "remote"},
}


def normalize_transcription(payload, call_id, event_id, *, leg=None):
    """`leg` names the call leg whose single inbound stream produced this event, when known.

    Only the Telnyx engine reports `transcription_track`; other engines (Soniox) omit it, so the
    speaker comes from the leg. Without either signal the side stays unknown.
    """
    data = payload.get("transcription_data", payload)
    track = (
        data.get("transcription_track")
        or data.get("track")
        or payload.get("transcription_track")
        or payload.get("transcription_tracks")
    )
    if track in {"remote", "rep"}:
        side = track
    elif leg in LEG_SIDES:
        side = LEG_SIDES[leg].get(track or "inbound", "unknown")
    else:
        side = {"inbound": "remote", "outbound": "rep"}.get(track, "unknown")
    text = data.get("transcript", data.get("text", ""))
    if not text:
        return []
    return [
        {
            "call_id": call_id,
            "segment_id": data.get("segment_id", event_id),
            "revision": data.get("revision", 1),
            "side": side,
            "speaker_id": data.get("speaker_id"),
            "start_ms": data.get("start_ms"),
            "end_ms": data.get("end_ms"),
            "text": text,
            "is_final": data.get("is_final", False),
            "confidence": data.get("confidence"),
            "content_hash": sha256(text.encode()).hexdigest(),
        }
    ]

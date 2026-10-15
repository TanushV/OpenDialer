from hashlib import sha256


def normalize_transcription(payload, call_id, event_id):
    data = payload.get("transcription_data", payload)
    track = data.get("track", payload.get("transcription_tracks"))
    side = {"inbound": "remote", "outbound": "rep", "remote": "remote", "rep": "rep"}.get(track, "unknown")
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

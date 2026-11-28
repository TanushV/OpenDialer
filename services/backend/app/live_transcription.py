"""Authenticated loopback audio for a current call; the carrier key stays in the backend."""

import asyncio
import contextlib
import logging
from time import monotonic
from typing import Literal
from uuid import uuid4

from fastapi import Body, Depends, WebSocket, WebSocketDisconnect

from app import call_records as recording
from app import live_help, ops
from app.calling import audio_tracks
from app.calling.state import TERMINAL
from app.helper.prospect_turn import ProspectTurn
from app.platform.db import emit, update
from app.providers import telnyx_stt
from app.service import fail

log = logging.getLogger(__name__)


def admission(service, p, call_id, *, finishing=False):
    if not service.managed.enabled(p["tenant_id"]):
        fail(409, "Direct speech requires a managed local workspace")
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        call = ops.required(service, s, "call_attempts", call_id, p)
        if call.data.get("rep_id") != p["user_id"]:
            fail(403, "Audio belongs to the caller")
        if (
            call.data.get("transcription_source") != "direct"
            or call.data.get("transcription_layout") != "direct_tracks"
        ):
            fail(409, "Transcription is not enabled for this call")
        if ops.speech_revoked(service, s, p, call):
            fail(403, "Transcription permission revoked")
        if not finishing and call.data["state"] in TERMINAL | {"ENDING"}:
            return False
        return dict(call.data)


def note_capture(service, p, call_id, side, start_ms):
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        recording.note_capture(service, s, p, call, side, start_ms)


def gap(service, p, call_id):
    """Mark missing speech on a live call; returns False for a call that already ended.

    Audio always stops a moment before the hangup event when the prospect hangs up, so a gap
    reported for an ended call is that ordinary ending, not missing speech.
    """
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        if call.data["state"] in TERMINAL | {"ENDING"} or call.data.get("ended_at"):
            return False
        update(
            call,
            analysis_incomplete=True,
            unprocessed_speech_events=call.data.get("unprocessed_speech_events", 0) + 1,
        )
        emit(s, p["tenant_id"], "call.updated", call, service.clock())
    service.managed.speech_failed = True
    return True


def bind_api(app, service, principal):
    active = set()

    @app.post("/v1/calls/{call_id}/playback")
    def report_playback(
        call_id: str,
        event: Literal[
            "ringback_playing", "ringback_unavailable", "remote_playing", "remote_unavailable"
        ] = Body(embed=True),
        p=Depends(principal),
    ):
        # Only playback status is stored in the local log, with no audio or device details.
        with service.db.transaction(False) as s:
            call = ops.required(service, s, "call_attempts", call_id, p)
            if call.data.get("rep_id") != p["user_id"]:
                fail(404, "Object not found")
        log.info("Call playback %s: %s", call_id, event)
        return {"status": "recorded"}

    @app.post("/v1/calls/{call_id}/audio-gap")
    def report_gap(call_id: str, body: dict | None = Body(default=None), p=Depends(principal)):
        admission(service, p, call_id, finishing=True)
        reason = str((body or {}).get("reason") or "unknown")[:40]
        recorded = gap(service, p, call_id)
        # Live check, November 10: a stopped transcript left no trace. Say why, once per call.
        if recorded:
            log.warning("Live transcription stopped on call %s: %s (reported by the Dialer)", call_id, reason)
        return {"status": "recorded" if recorded else "ignored"}

    @app.websocket("/v1/calls/{call_id}/audio/{side}")
    async def audio(ws: WebSocket, call_id: str, side: str):
        """One side of a live call: kept on disk when the call may keep it, transcribed when it may be (plan P1, P2).

        A side that may not be transcribed yet still gives the helper its pause timing and the call its voice
        activity, both computed here; when the rep presses R the prospect's transcript starts mid-stream.
        """
        if side not in {"rep", "remote"} or (
            ws.headers.get("origin") and ws.headers["origin"] not in service.settings.allowed_origins
        ):
            await ws.close(code=4403)
            return
        await ws.accept()
        identity, p, clean, track, tracks = None, None, False, None, None
        try:
            auth = await asyncio.wait_for(ws.receive_json(), 10)
            p = await asyncio.to_thread(
                service.sessions.check, service.db, auth.get("token", ""), service.clock()
            )
            call = await asyncio.to_thread(admission, service, p, call_id)
            if not call:
                fail(409, "Call has ended")
            identity = (p["tenant_id"], call_id, side)
            if identity in active:
                identity = None
                fail(409, "This call already has an audio stream")
            active.add(identity)
            key = service.configs.read(p["tenant_id"]).get("telnyx_api_key")
            transcribe, _ = recording.side_policy(call, side, live=bool(call.get("live_speech")))
            if transcribe and not key:
                fail(409, "Telnyx key is missing")

            async def ready():
                service.managed.speech_failed = False  # Speech works again on this call.
                await ws.send_json({"status": "ready"})

            started, total = monotonic(), 0
            turn = ProspectTurn() if side == "remote" else None
            tracks, track = audio_tracks.open_side(p["tenant_id"], call_id, side, recording.folder(service))
            noted = False
            state = {"transcribe": transcribe}

            async def dispatch_turn():
                if turn is not None and (candidate := turn.take()) is not None:
                    version, segment_id = candidate
                    await asyncio.to_thread(
                        live_help.queue_confirmed_turn, service, p, call_id, version, segment_id
                    )

            async def frames():
                nonlocal total, clean, noted
                while True:
                    # No blocking database work or unbounded audio buffers on the event loop.
                    current = await asyncio.to_thread(admission, service, p, call_id)
                    if not current:
                        clean = True
                        return
                    state["transcribe"], keep = recording.side_policy(
                        current, side, live=bool(current.get("live_speech"))
                    )
                    await asyncio.to_thread(
                        service.sessions.check, service.db, auth["token"], service.clock()
                    )
                    try:
                        message = await asyncio.wait_for(ws.receive(), 2)
                    except TimeoutError:
                        continue
                    if message["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect()
                    if message.get("text") == '{"stop":true}':
                        clean = True
                        return
                    data = message.get("bytes")
                    if not isinstance(data, bytes) or not 0 < len(data) <= 16384 or len(data) % 2:
                        raise ValueError("Invalid audio frame")
                    total += len(data)
                    if total > 32000 * (monotonic() - started + 2):
                        raise ValueError("Audio buffer exceeded")
                    chunk = audio_tracks.accept(tracks, track, data, keep=keep)
                    if keep and not noted:
                        noted = True
                        await asyncio.to_thread(note_capture, service, p, call_id, side, track.kept_start_ms)
                    if chunk:
                        await asyncio.to_thread(audio_tracks.write_part, tracks, track, chunk)
                    if turn is not None:
                        turn.observe(data)
                        await dispatch_turn()
                    yield data

            source = frames()
            if not state["transcribe"]:
                # Local only for now: timing, voice activity and kept audio; no audio leaves the computer.
                await ready()
                async for _ in source:
                    if state["transcribe"]:
                        break
                else:
                    await ws.send_json({"status": "stopped"})
                    await ws.close()
                    return
                if not key:
                    fail(409, "Telnyx key is missing")
            segments = telnyx_stt.Segments(str(uuid4()), side)
            heard = {}
            async for frame in telnyx_stt.transcribe(key, source, on_ready=ready):
                await asyncio.to_thread(admission, service, p, call_id, finishing=True)
                segment = segments.accept(frame, service.clock())
                if segment:
                    received = segment.pop("first_received")
                    # Call-relative timing from this side's own audio position (plan P1, metrics).
                    at = track.position_ms(tracks.offset_ms(track))
                    begun = heard.setdefault(segment["segment_id"], max(0, at - 1200))
                    segment.update(start_ms=begun, end_ms=max(begun, at - 500))
                    if segment["is_final"]:
                        heard.pop(segment["segment_id"], None)
                    if turn is not None:
                        # Wait for this revision to commit before dispatching any pending tip.
                        turn.committing = True
                    result = await ops.transcript(
                        service, p, call_id, segment, received_at=received, queue_helper=turn is None
                    )
                    if turn is not None:
                        usable = segment["text"].strip() and not (
                            result.get("expired") or result.get("duplicate")
                        )
                        turn.committed(
                            result["transcript_version"] if usable else None,
                            segment["segment_id"],
                            segment["text"],
                        )
                        await dispatch_turn()
                    await ws.send_json({"status": "transcribed"})
            await ws.send_json({"status": "stopped"})
            await ws.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "Live transcription socket for call %s (%s) stopped: %s %s",
                call_id,
                side,
                type(exc).__name__,
                str(getattr(exc, "detail", "") or exc)[:200],
            )
            if identity and p and not clean:
                await asyncio.to_thread(gap, service, p, call_id)
            try:
                if identity:
                    await ws.send_json(
                        {
                            "status": "unavailable",
                            "message": "Live transcription unavailable; check calling setup.",
                        }
                    )
                await ws.close(code=4401)
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            if track is not None:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(
                        audio_tracks.write_part, tracks, track, audio_tracks.drain(tracks, track)
                    )
                track.closed = True
            if identity:
                active.discard(identity)

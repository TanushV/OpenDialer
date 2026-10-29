"""Authenticated loopback audio for a current call; the carrier key stays in the backend."""

import asyncio
from time import monotonic
from uuid import uuid4

from fastapi import Depends, WebSocket, WebSocketDisconnect

from app.db import emit, update
from app.providers import telnyx_stt
from app.security import decode_session
from app.service import fail
from app.telephony.reducer import TERMINAL


def admission(service, p, call_id, *, finishing=False):
    if not service.managed.enabled(p["tenant_id"]):
        fail(409, "Direct speech requires a managed local workspace")
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        call = service.required(s, "call_attempts", call_id, p)
        if call.data.get("rep_id") != p["user_id"]:
            fail(403, "Audio belongs to the caller")
        if (
            call.data.get("transcription_source") != "direct"
            or call.data.get("transcription_layout") != "direct_tracks"
        ):
            fail(409, "Transcription is not enabled for this call")
        if service.speech_revoked(s, p, call):
            fail(403, "Transcription permission revoked")
        if not finishing and call.data["state"] in TERMINAL | {"ENDING"}:
            return False
        return True


def gap(service, p, call_id):
    with service.db.transaction() as s:
        call = service.required(s, "call_attempts", call_id, p)
        update(
            call,
            analysis_incomplete=True,
            unprocessed_speech_events=call.data.get("unprocessed_speech_events", 0) + 1,
        )
        emit(s, p["tenant_id"], "call.updated", call, service.clock())
    service.managed.speech_failed = True


def bind_api(app, service, principal):
    active = set()

    @app.post("/v1/calls/{call_id}/audio-gap")
    def report_gap(call_id: str, p=Depends(principal)):
        admission(service, p, call_id, finishing=True)
        gap(service, p, call_id)
        return {"status": "recorded"}

    @app.websocket("/v1/calls/{call_id}/audio/{side}")
    async def audio(ws: WebSocket, call_id: str, side: str):
        if (
            side not in {"rep", "remote"}
            or service.settings.mode != "local"
            or (ws.headers.get("origin") and ws.headers["origin"] not in service.settings.allowed_origins)
        ):
            await ws.close(code=4403)
            return
        await ws.accept()
        identity, p, clean = None, None, False
        try:
            auth = await asyncio.wait_for(ws.receive_json(), 10)
            p = await asyncio.to_thread(
                decode_session, service.db, service.settings, auth.get("token", ""), service.clock()
            )
            if not await asyncio.to_thread(admission, service, p, call_id):
                fail(409, "Call has ended")
            identity = (p["tenant_id"], call_id, side)
            if identity in active:
                identity = None
                fail(409, "This call already has an audio stream")
            active.add(identity)
            key = service.configs.read(p["tenant_id"]).get("telnyx_api_key")
            if not key:
                fail(409, "Telnyx key is missing")

            async def ready():
                await ws.send_json({"status": "ready"})

            started, total = monotonic(), 0

            async def frames():
                nonlocal total, clean
                while True:
                    # No blocking database work or unbounded audio buffers on the event loop.
                    if not await asyncio.to_thread(admission, service, p, call_id):
                        clean = True
                        return
                    await asyncio.to_thread(
                        decode_session, service.db, service.settings, auth["token"], service.clock()
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
                    yield data

            segments = telnyx_stt.Segments(str(uuid4()), side)
            async for frame in telnyx_stt.transcribe(key, frames(), on_ready=ready):
                await asyncio.to_thread(admission, service, p, call_id, finishing=True)
                segment = segments.accept(frame, service.clock())
                if segment:
                    received = segment.pop("first_received")
                    await service.transcript(p, call_id, segment, received_at=received)
                    await ws.send_json({"status": "transcribed"})
            await ws.send_json({"status": "stopped"})
            await ws.close()
        except asyncio.CancelledError:
            raise
        except Exception:
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
            if identity:
                active.discard(identity)

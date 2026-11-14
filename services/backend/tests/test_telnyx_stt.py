"""Telnyx's documented unified WebSocket frames; no live carrier/account claim."""

from datetime import timedelta

import pytest


def test_interim_revisions_share_identity_until_final_then_advance():
    from app.providers.telnyx_stt import Segments

    stream = Segments("session", "remote")
    first = stream.accept({"transcript": "Do not", "is_final": False}, 10)
    final = stream.accept({"transcript": "Do not call me", "is_final": True, "confidence": 0.95}, 11)
    next_segment = stream.accept({"transcript": "Thank you", "is_final": True}, 12)
    assert first["segment_id"] == final["segment_id"]
    assert final["revision"] == 2 and final["is_final"]
    assert next_segment["segment_id"] != final["segment_id"]
    assert final["side"] == "remote" and final["first_received"] == 10


def test_empty_error_and_unrecognized_frames_never_become_speech():
    from app.providers.telnyx_stt import Segments

    stream = Segments("session", "rep")
    assert stream.accept({"transcript": "", "is_final": True}, 10) is None
    with pytest.raises(ValueError):
        stream.accept({"error": "provider error with arbitrary body"}, 10)
    with pytest.raises(ValueError):
        stream.accept({"transcript": "text", "is_final": "false"}, 10)
    with pytest.raises(ValueError):
        stream.accept({"transcript": "x" * 20001, "is_final": True}, 10)


async def test_stream_closes_and_flushes_without_logging_or_saving_audio(monkeypatch):
    import json

    from app.providers import telnyx_stt

    sent = []

    class Socket:
        async def send(self, data):
            sent.append(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            import asyncio

            while not sent or not isinstance(sent[-1], str):
                await asyncio.sleep(0)
            if getattr(self, "done", False):
                raise StopAsyncIteration
            self.done = True
            return json.dumps({"transcript": "final words", "is_final": True})

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            pass

    def connect(url, **kwargs):
        assert "transcription_engine=Soniox" in url
        assert "not-a-real-key" not in url
        assert kwargs["additional_headers"]["Authorization"] == "Bearer not-a-real-key"
        return Connection()

    monkeypatch.setattr(telnyx_stt, "connect", connect)

    async def frames():
        yield b"\0\0" * 800

    output = [value async for value in telnyx_stt.transcribe("not-a-real-key", frames())]
    assert output == [{"transcript": "final words", "is_final": True}]
    assert sent[0] == b"\0\0" * 800 and json.loads(sent[-1]) == {"type": "CloseStream"}


def test_live_socket_requires_a_current_local_call(workspace, app):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with workspace["client"].websocket_connect("/v1/calls/absent/audio/rep") as ws:
            ws.send_json({"token": workspace["access_token"]})
            ws.receive_json()


def managed_call(workspace, app):
    from time import monotonic

    from app.platform.db import get, update
    from conftest import start

    service = app.state.service
    service.managed.config = {"tenant_id": workspace["tenant_id"], "workspace_id": "test"}
    service.managed.last_exchange = monotonic()
    service.managed.caught_up = True
    service.managed.numbers = {"+14155550100"}
    service.configs.write(workspace["tenant_id"], {"telnyx_api_key": "not-a-real-key"})
    made = start(workspace)
    with service.db.transaction() as s:
        call = get(s, "call_attempts", made["id"], workspace["tenant_id"])
        update(call, state="HUMAN", transcription_layout="direct_tracks")
    return made


def test_managed_socket_delivers_speech_to_existing_pipeline(workspace, app, monkeypatch):
    from app.providers import telnyx_stt

    made = managed_call(workspace, app)

    async def fake(key, frames, *, on_ready=None):
        await on_ready()
        async for frame in frames:
            assert len(frame) == 1600
            yield {"transcript": "We have no budget this quarter", "is_final": True}

    monkeypatch.setattr(telnyx_stt, "transcribe", fake)
    with workspace["client"].websocket_connect(f"/v1/calls/{made['id']}/audio/remote") as ws:
        ws.send_json({"token": workspace["access_token"]})
        assert ws.receive_json()["status"] == "ready"
        ws.send_bytes(b"\0\0" * 800)
        assert ws.receive_json()["status"] == "transcribed"
        ws.send_json({"stop": True})
    detail = workspace["client"].get(f"/v1/calls/{made['id']}", headers=workspace["headers"]).json()
    assert any(
        row["text"] == "We have no budget this quarter" and row["side"] == "remote"
        for row in detail["transcript"]
    )


@pytest.mark.slow
def test_prospect_pause_and_final_text_release_one_helper_request(workspace, app, clock, monkeypatch):
    from math import pi, sin
    from struct import pack
    from threading import Semaphore

    from app import live_help
    from app import live_transcription as audio
    from app.providers import telnyx_stt

    made = managed_call(workspace, app)
    path = f"/v1/calls/{made['id']}"

    requests = []

    class Selector:
        async def converse(self, messages, **_):
            requests.append(messages[1]["content"])
            return "Part: 1"

    monkeypatch.setattr(live_help, "provider", lambda *_: Selector())
    start_at = clock.now
    monkeypatch.setattr(audio, "monotonic", lambda: (clock.now - start_at).total_seconds())
    frames_by_packet = {
        1: ("We’re interested, but", True),
        6: ("we would need", False),
        16: ("we would need approval first.", True),
        21: ("From finance.", True),
        28: ("Yes.", True),
    }
    consumed = Semaphore(0)

    async def fake(key, frames, *, on_ready=None):
        await on_ready()
        packet = 0
        async for _ in frames:
            packet += 1
            if packet in frames_by_packet:
                transcript, final = frames_by_packet[packet]
                yield {"transcript": transcript, "is_final": final}
            consumed.release()

    monkeypatch.setattr(telnyx_stt, "transcribe", fake)
    client, headers = workspace["client"], workspace["headers"]

    def drain():
        response = client.post("/v1/admin/jobs/run", headers=headers)
        assert response.status_code == 200

    voiced = pack("<320h", *(int(4500 * sin(2 * pi * 440 * i / 16000)) for i in range(320)))
    speech = voiced * 5
    silence = bytes(3200)
    with client.websocket_connect(path + "/audio/remote") as stream:
        stream.send_json({"token": workspace["access_token"]})
        assert stream.receive_json()["status"] == "ready"

        packet_number = 0

        def send(packet, n=1):
            nonlocal packet_number
            for _ in range(n):
                packet_number += 1
                clock.now += timedelta(milliseconds=100)
                stream.send_bytes(packet)
                assert consumed.acquire(timeout=2)
                if packet_number in frames_by_packet:
                    assert stream.receive_json()["status"] == "transcribed"

        send(speech)
        send(silence, 4)
        send(speech, 2)
        drain()
        assert requests == [], "A mid-sentence pause must not spend a request"
        send(silence, 9)
        drain()
        assert len(requests) == 1 and "approval first" in requests[0]
        assert (
            client.get(path, headers=headers).json()["coach"]["say_or_ask"]
            == made["script_snapshot"]["opener"]
        )
        send(silence, 10)
        drain()
        assert len(requests) == 1, "Quiet audio and additional final fragments share the same turn"
        send(speech)
        send(silence)
        drain()
        assert len(requests) == 1, "A final segment alone cannot confirm the short turn"
        send(silence, 6)
        drain()
        assert len(requests) == 2 and "Yes." in requests[1]
        stream.send_json({"stop": True})

"""Telnyx's documented unified WebSocket frames; no live carrier/account claim."""

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

"""Direct Telnyx Soniox transport; audio and transcripts remain in bounded memory."""

import asyncio
import json
from hashlib import sha256
from time import monotonic
from urllib.parse import urlencode

from websockets.asyncio.client import connect

ENDPOINT = "wss://api.telnyx.com/v2/speech-to-text/transcription?" + urlencode(
    {
        "transcription_engine": "Soniox",
        "language": "en",
        "input_format": "linear16",
        "sample_rate": 16000,
        "interim_results": "true",
        "endpointing": 1000,
    }
)


class Segments:
    """The unified Telnyx protocol emits revisions followed by one final utterance."""

    def __init__(self, identity, side):
        if side not in {"remote", "rep"}:
            raise ValueError("Unknown audio side")
        self.identity, self.side = identity, side
        self.sequence, self.revision, self.first_received = 0, 0, None

    def accept(self, frame, received):
        if not isinstance(frame, dict) or frame.get("error"):
            raise ValueError("Transcription provider failed")
        text = frame.get("transcript")
        if text == "":
            return None
        if not isinstance(text, str) or len(text) > 20000 or type(frame.get("is_final")) is not bool:
            raise ValueError("Unrecognized transcription response")
        if self.first_received is None:
            self.first_received = received
        self.revision += 1
        confidence = frame.get("confidence")
        result = {
            "segment_id": f"{self.identity}:{self.sequence}",
            "revision": self.revision,
            "side": self.side,
            "text": text,
            "is_final": frame["is_final"],
            "confidence": confidence
            if isinstance(confidence, (int, float)) and 0 <= confidence <= 1
            else None,
            "content_hash": sha256(text.encode()).hexdigest(),
            "first_received": self.first_received,
        }
        if frame["is_final"]:
            self.sequence += 1
            self.revision, self.first_received = 0, None
        return result


async def transcribe(key, frames, *, on_ready=None):
    """Caller owns the authorization/lifetime. Bounded frames are PCM16 mono at 16 kHz.

    The audio iterator must stop when its call ends. A clean stop allows two seconds for
    final speech. Socket failures propagate as gaps; there is no replay or hidden fallback.
    """
    async with connect(
        ENDPOINT,
        additional_headers={"Authorization": "Bearer " + key},
        open_timeout=10,
        close_timeout=2,
        max_size=65536,
        max_queue=4,
    ) as socket:
        if on_ready:
            await on_ready()
        finished_at = None

        async def upload():
            nonlocal finished_at
            async for frame in frames:
                if not isinstance(frame, bytes) or not 0 < len(frame) <= 16384 or len(frame) % 2:
                    raise ValueError("Invalid PCM frame")
                await socket.send(frame)
            await socket.send(json.dumps({"type": "CloseStream"}))
            finished_at = monotonic()

        sending = asyncio.create_task(upload())
        iterator = socket.__aiter__()
        receiving = None
        try:
            while True:
                receiving = asyncio.create_task(anext(iterator))
                if not sending.done():
                    await asyncio.wait({sending, receiving}, return_when=asyncio.FIRST_COMPLETED)
                if sending.done():
                    sending.result()
                    try:
                        message = await asyncio.wait_for(receiving, max(0, finished_at + 2 - monotonic()))
                    except TimeoutError:
                        return
                else:
                    message = await receiving
                if not isinstance(message, str):
                    raise ValueError("Unexpected transcription frame")
                yield json.loads(message)
        except StopAsyncIteration:
            if not sending.done():
                raise ValueError("Transcription ended before audio completed") from None
            sending.result()
            return
        finally:
            sending.cancel()
            if receiving:
                receiving.cancel()
            await asyncio.gather(sending, *([receiving] if receiving else []), return_exceptions=True)

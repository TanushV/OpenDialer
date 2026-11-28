"""The live audio of each call in this process: where each side is in time, how loud it is and what it keeps.

Both sides arrive as 16 kHz mono PCM16 on their own sockets (`live_transcription.py`). Positions are call-relative:
milliseconds since the first frame of either side, so transcript segments, voice activity and the saved stereo file
share one clock. Only sides the call may keep are written to disk (call insights plan P1), as temporary PCM parts
that `recording.finalize` turns into one encrypted file. Nothing here leaves the computer.
"""

import math
import threading
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

BYTES_PER_MS = 32  # 16 kHz, 16-bit mono
ENVELOPE_MS = 100
SPEECH_DBFS = -45.0
FLUSH_BYTES = 64000


@dataclass
class Side:
    name: str
    started: float
    received: int = 0
    # Loudness (dBFS, rounded) of each 100 ms of this side, from its first frame.
    envelope: array = field(default_factory=lambda: array("b"))
    pending: bytearray = field(default_factory=bytearray)
    keep_from_ms: int | None = None
    kept_start_ms: int | None = None
    kept_bytes: int = 0
    closed: bool = False
    _partial: bytearray = field(default_factory=bytearray)

    def position_ms(self, offset_ms):
        return offset_ms + self.received // BYTES_PER_MS


@dataclass
class CallAudio:
    base: float
    folder: Path
    call_id: str
    sides: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def offset_ms(self, side):
        return round((side.started - self.base) * 1000)

    def part(self, name):
        return self.folder / f"{self.call_id}.{name}.pcm"


_CALLS: dict[tuple[str, str], CallAudio] = {}
_LOCK = threading.Lock()


def open_side(scope, call_id, name, folder, now=None):
    """Register one side's socket; the first side to arrive sets the call's clock."""
    now = monotonic() if now is None else now
    with _LOCK:
        audio = _CALLS.get((scope, call_id))
        if audio is None:
            audio = _CALLS[(scope, call_id)] = CallAudio(base=now, folder=Path(folder), call_id=call_id)
        side = Side(name=name, started=now)
        audio.sides[name] = side
        return audio, side


def current(scope, call_id):
    return _CALLS.get((scope, call_id))


def forget(scope, call_id):
    with _LOCK:
        return _CALLS.pop((scope, call_id), None)


def dbfs(frame):
    samples = array("h")
    samples.frombytes(frame[: len(frame) - len(frame) % 2])
    if not samples:
        return -100
    power = sum(value * value for value in samples) / len(samples)
    return max(-100, round(10 * math.log10(power / (32768.0**2)))) if power else -100


def accept(audio, side, frame, *, keep):
    """Note one frame: its loudness, and its bytes when this side's audio may be kept from here on.

    Returns PCM ready to append to the side's part file (in `FLUSH_BYTES` chunks), or b"".
    """
    with audio.lock:
        offset = audio.offset_ms(side)
        side._partial.extend(frame)
        window = ENVELOPE_MS * BYTES_PER_MS
        while len(side._partial) >= window:
            side.envelope.append(dbfs(bytes(side._partial[:window])))
            del side._partial[:window]
        position = side.position_ms(offset)
        side.received += len(frame)
        if keep and side.keep_from_ms is None:
            side.keep_from_ms = position
        if not keep or side.keep_from_ms is None:
            return b""
        if side.kept_start_ms is None:
            side.kept_start_ms = position
        side.pending.extend(frame)
        side.kept_bytes += len(frame)
        if len(side.pending) < FLUSH_BYTES:
            return b""
        chunk, side.pending = bytes(side.pending), bytearray()
        return chunk


def drain(audio, side):
    with audio.lock:
        chunk, side.pending = bytes(side.pending), bytearray()
        return chunk


def write_part(audio, side, chunk):
    if chunk:
        audio.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(audio.part(side.name), "ab") as stream:
            stream.write(chunk)


def activity(audio, name):
    """Call-relative [start_ms, end_ms] runs where a side was speaking, from its loudness envelope."""
    side = audio.sides.get(name)
    if side is None:
        return []
    offset = audio.offset_ms(side)
    runs, start, quiet = [], None, 0
    for index, level in enumerate(side.envelope):
        at = offset + index * ENVELOPE_MS
        if level >= SPEECH_DBFS:
            if start is None:
                start = at
            quiet = 0
        elif start is not None:
            quiet += 1
            # A 300 ms hangover keeps one utterance whole across short gaps between words.
            if quiet > 3:
                runs.append([start, at - (quiet - 1) * ENVELOPE_MS])
                start, quiet = None, 0
    if start is not None:
        runs.append([start, offset + len(side.envelope) * ENVELOPE_MS])
    return runs


def level(audio, name, start_ms, end_ms):
    """Mean loudness of a side over a call-relative window, or None when it has no audio there."""
    side = audio.sides.get(name)
    if side is None:
        return None
    offset = audio.offset_ms(side)
    first = max(0, (start_ms - offset) // ENVELOPE_MS)
    last = max(first, (end_ms - offset) // ENVELOPE_MS)
    values = list(side.envelope[first : last + 1])
    return sum(values) / len(values) if values else None


def bleed_suspect(audio, start_ms, end_ms):
    """A rep segment that sits inside prospect speech and is quieter than it: the prospect's voice in the rep's
    microphone (plan C6), for calls where the prospect's words are not transcribed to compare."""
    if audio is None or start_ms is None or end_ms is None or end_ms <= start_ms:
        return False
    spoken = sum(max(0, min(end_ms, run[1]) - max(start_ms, run[0])) for run in activity(audio, "remote"))
    if spoken < 0.8 * (end_ms - start_ms):
        return False
    rep, remote = level(audio, "rep", start_ms, end_ms), level(audio, "remote", start_ms, end_ms)
    return rep is not None and remote is not None and rep <= remote - 6

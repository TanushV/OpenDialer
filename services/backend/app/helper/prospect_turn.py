"""Confirm prospect pauses on the existing PCM stream before starting the live helper."""

from webrtcvad import Vad

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
PAUSE_MS = 500
RESUMED_MS = 200


class ProspectTurn:
    """Only the remote audio socket owns this state. Keep no audio history beyond one partial frame."""

    def __init__(self):
        self.vad = Vad(1)
        self.tail = b""
        self.quiet_ms = 0
        self.interim = False
        self.dispatched = False
        self.pending = None

    def observe(self, packet):
        pcm = self.tail + packet
        complete = len(pcm) // FRAME_BYTES * FRAME_BYTES
        self.tail = pcm[complete:]
        for start in range(0, complete, FRAME_BYTES):
            if self.vad.is_speech(pcm[start : start + FRAME_BYTES], RATE):
                # A new stretch of speech needs its own final text. Earlier fragments are already in context.
                if self.quiet_ms >= RESUMED_MS:
                    self.pending = None
                self.quiet_ms = 0
                self.dispatched = False
            else:
                self.quiet_ms = min(PAUSE_MS, self.quiet_ms + FRAME_MS)

    def take(self):
        """Release the newest final fragment once per pause, after outstanding interim text settles."""
        if self.interim or self.pending is None or self.dispatched or self.quiet_ms < PAUSE_MS:
            return None
        version, self.pending = self.pending, None
        self.dispatched = True
        return version

    def finalized(self, version=None):
        self.interim = False
        if version is not None and not self.dispatched:
            self.pending = version

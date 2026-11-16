"""Confirm prospect pauses on the existing PCM stream before starting the live helper."""

from webrtcvad import Vad

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
PAUSE_MS = 400
RESUMED_MS = 200


class ProspectTurn:
    """Only the remote audio socket owns this state. Keep no audio history beyond one partial frame."""

    def __init__(self):
        self.vad = Vad(1)
        self.tail = b""
        self.quiet_ms = 0
        self.committing = False
        self.dispatched = False
        self.pending = None
        self.sent_segment = None

    def observe(self, packet):
        pcm = self.tail + packet
        complete = len(pcm) // FRAME_BYTES * FRAME_BYTES
        self.tail = pcm[complete:]
        for start in range(0, complete, FRAME_BYTES):
            if self.vad.is_speech(pcm[start : start + FRAME_BYTES], RATE):
                # Clear the prior candidate when new speech starts after a pause.
                if self.quiet_ms >= RESUMED_MS:
                    self.pending = None
                    self.dispatched = False
                self.quiet_ms = 0
            else:
                self.quiet_ms = min(PAUSE_MS, self.quiet_ms + FRAME_MS)

    def take(self):
        """Release the latest committed text once local audio confirms a pause."""
        if self.committing or self.pending is None or self.dispatched or self.quiet_ms < PAUSE_MS:
            return None
        version, self.sent_segment = self.pending
        self.pending = None
        self.dispatched = True
        return version, self.sent_segment

    def committed(self, version, segment_id):
        self.committing = False
        # A final revision of a segment already used by the helper is not a new turn.
        if version is not None and not self.dispatched and segment_id != self.sent_segment:
            self.pending = version, segment_id

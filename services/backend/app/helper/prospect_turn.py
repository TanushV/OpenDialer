"""Confirm prospect pauses on the existing PCM stream before starting the live helper."""

import re

from webrtcvad import Vad

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
PAUSE_MS = 240
UNFINISHED_PAUSE_MS = 540
CONNECTORS = frozenset("a an and at because but by for from if in of on or so than that the to with".split())


class ProspectTurn:
    """Only the remote audio socket owns this state. Keep no audio history beyond one partial frame."""

    def __init__(self):
        self.vad = Vad(1)
        self.tail = b""
        self.quiet_ms = 0
        self.committing = False
        self.dispatched = False
        self.pending = None
        self.sent_segment, self.sent_words = None, 0

    def observe(self, packet):
        pcm = self.tail + packet
        complete = len(pcm) // FRAME_BYTES * FRAME_BYTES
        self.tail = pcm[complete:]
        for start in range(0, complete, FRAME_BYTES):
            if self.vad.is_speech(pcm[start : start + FRAME_BYTES], RATE):
                # A pause we already answered ends when the prospect speaks again.
                if self.quiet_ms >= PAUSE_MS:
                    self.dispatched = False
                self.quiet_ms = 0
            else:
                self.quiet_ms = min(UNFINISHED_PAUSE_MS, self.quiet_ms + FRAME_MS)

    def take(self):
        """Release the latest committed text once local audio confirms a pause."""
        if self.committing or self.pending is None or self.dispatched:
            return None
        version, segment_id, words, wait_ms = self.pending
        if self.quiet_ms < wait_ms:
            return None
        self.sent_segment, self.sent_words = segment_id, words
        self.pending = None
        self.dispatched = True
        return version, self.sent_segment

    def committed(self, version, segment_id, text):
        self.committing = False
        if version is None or self.dispatched or not (words := re.findall(r"\b\w+\b", text.casefold())):
            return
        # A late final can repeat the same segment. After speech resumes, accept any added words.
        if segment_id == self.sent_segment and len(words) <= self.sent_words:
            return
        ending = text.rstrip()
        unfinished = ending.endswith((",", "–", "—", "…")) or (
            not ending.endswith("?") and words[-1] in CONNECTORS
        )
        self.pending = version, segment_id, len(words), UNFINISHED_PAUSE_MS if unfinished else PAUSE_MS

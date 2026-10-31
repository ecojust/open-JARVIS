"""Bounded, VAD-gated microphone metering without an animation timer.

The capture consumer supplies an existing VAD decision. Only eligible speech
frames enter NumPy; silence and paused capture never load it for metering.
No samples are retained, recorded, or sent with the returned UI event.
"""

from __future__ import annotations

import math
import threading
import time


MAX_FRAME_BYTES = 1920


class SpeechMeter:
    """Return real PCM level changes, capped at 12.5 Hz by default.

    Zero transitions are immediate and are emitted once. They do not reset the
    speech deadline, so alternating speech/silence cannot bypass the rate cap.
    This object returns data only; callers must emit outside its internal lock.
    """

    def __init__(self, clock=time.monotonic, max_hz=12.5):
        if (
            type(max_hz) not in (int, float)
            or not math.isfinite(max_hz)
            or not 0 < max_hz <= 15
        ):
            raise ValueError("Meter frequency must be greater than 0 and at most 15 Hz")
        if not callable(clock):
            raise ValueError("Meter clock must be callable")
        self._clock = clock
        self._interval = 1.0 / max_hz
        self._lock = threading.Lock()
        self._last_measurement_at = -math.inf
        self._state = (0.0, False, False)

    @staticmethod
    def _event(state):
        level, speech, listening = state
        return {"level": level, "speech": speech, "listening": listening}

    def _zero(self, listening):
        state = (0.0, False, listening)
        if state == self._state:
            return None
        self._state = state
        return self._event(state)

    def reset(self, listening=False):
        if type(listening) is not bool:
            raise ValueError("Listening state must be a boolean")
        with self._lock:
            return self._zero(listening)

    def feed(self, pcm: bytes, is_speech: bool, listening=True):
        if (
            type(pcm) is not bytes
            or not 0 < len(pcm) <= MAX_FRAME_BYTES
            or len(pcm) % 2
        ):
            raise ValueError("Expected a bounded, non-empty mono int16 PCM frame")
        if type(is_speech) is not bool or type(listening) is not bool:
            raise ValueError("Speech and listening states must be booleans")
        with self._lock:
            # VAD silence, pause, or an all-zero frame clears an active display
            # immediately. bytes.count runs natively, including VAD hangover.
            if not listening or not is_speech or pcm.count(b"\x00") == len(pcm):
                return self._zero(listening)
            now = self._clock()
            if now - self._last_measurement_at < self._interval:
                return None
            self._last_measurement_at = now
            import numpy as np

            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            rms = math.sqrt(float(np.dot(samples, samples)) / samples.size) / 32768.0
            # A fixed dBFS scale preserves real amplitude differences without
            # automatic gain that would make quiet background noise look loud.
            level = max(0.0, min(1.0, (20 * math.log10(rms) + 60) / 60))
            if level == 0:
                return self._zero(True)
            self._state = (level, True, True)
            return self._event(self._state)

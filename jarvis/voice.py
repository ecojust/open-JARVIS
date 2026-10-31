"""Pure PCM utterance segmentation, independent of microphone and STT engines.

The caller supplies fixed-size mono int16 PCM frames and the VAD decision for
each frame. No audio devices, native packages, or model weights are imported.
"""

from collections import deque
import math


class Segmenter:
    """Collect speech with short pre-roll and finish after sustained silence.

    ``feed`` returns one complete utterance, or ``None`` while collecting or
    discarding a short noise burst. Returned audio includes pre-roll and ending
    silence. A long utterance is split at the maximum duration, bounding memory
    use even when the VAD continuously detects speech.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        pre_roll_ms: int = 300,
        silence_ms: int = 900,
        min_speech_ms: int = 300,
        max_segment_ms: int = 20000,
    ):
        parameters = (
            sample_rate,
            frame_ms,
            pre_roll_ms,
            silence_ms,
            min_speech_ms,
            max_segment_ms,
        )
        if any(type(value) is not int for value in parameters):
            raise ValueError("Audio segmentation parameters must be integers")
        if sample_rate not in (8000, 16000, 32000, 48000) or frame_ms not in (
            10,
            20,
            30,
        ):
            raise ValueError("Use a WebRTC VAD sample rate and 10, 20, or 30 ms frames")
        if pre_roll_ms < 0 or silence_ms <= 0 or min_speech_ms <= 0:
            raise ValueError(
                "Pre-roll must be non-negative; silence and minimum speech must be positive"
            )
        self.frame_bytes = sample_rate * frame_ms // 1000 * 2
        self._pre_roll_frames = math.ceil(pre_roll_ms / frame_ms)
        self._silence_frames = math.ceil(silence_ms / frame_ms)
        self._min_speech_frames = math.ceil(min_speech_ms / frame_ms)
        self._max_frames = max_segment_ms // frame_ms
        if self._max_frames < self._pre_roll_frames + self._min_speech_frames:
            raise ValueError(
                "Maximum duration must fit pre-roll and the minimum speech duration"
            )
        self._pre_roll = deque(maxlen=self._pre_roll_frames)
        self.reset()

    def reset(self) -> None:
        """Discard all buffered audio, e.g. after pausing or an input gap."""
        self._pre_roll.clear()
        self._frames: list[bytes] = []
        self._voiced = 0
        self._silent = 0

    def feed(self, pcm: bytes, is_speech: bool) -> bytes | None:
        if not isinstance(pcm, bytes) or len(pcm) != self.frame_bytes:
            raise ValueError(
                f"Expected exactly {self.frame_bytes} bytes of mono int16 PCM"
            )
        if type(is_speech) is not bool:
            raise ValueError("The VAD decision must be a boolean")

        if not self._frames:
            if not is_speech:
                self._pre_roll.append(pcm)
                return None
            self._frames = list(self._pre_roll)
            self._pre_roll.clear()

        self._frames.append(pcm)
        if is_speech:
            self._voiced += 1
            self._silent = 0
        else:
            self._silent += 1

        if self._silent < self._silence_frames and len(self._frames) < self._max_frames:
            return None

        result = (
            b"".join(self._frames) if self._voiced >= self._min_speech_frames else None
        )
        self.reset()
        return result

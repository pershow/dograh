"""Synchronous per-call VAD, disjoint windows and speaker hypotheses.

All methods run in a bounded executor, never on the call's event loop.
"""

import time
from importlib.resources import files

import numpy as np

from api.services.speaker_tracking.model import embed, load_extractor
from api.services.speaker_tracking.tracker import SpeakerTracker


class CallAnalyzer:
    def __init__(self):
        from pipecat.audio.vad.silero import SileroOnnxModel

        load_extractor()
        self.vad = SileroOnnxModel(
            str(files("pipecat.audio.vad.data").joinpath("silero_vad.onnx")),
            force_onnx_cpu=True,
        )
        self.tracker = SpeakerTracker()
        self._format = None
        self.reset()

    def reset(self, *, break_sequence=True):
        self._pending = b""
        self._window: list[np.ndarray] = []
        self._speech_blocks = 0
        self._silence_blocks = 0
        self.vad.reset_states()
        if break_sequence:
            self.tracker.break_sequence()

    def process(
        self, pcm: bytes, rate: int, end: float, learn: bool, gap: bool
    ) -> list[dict]:
        if gap or self._format != (rate, learn):
            self.reset(
                break_sequence=gap or self._format is None or self._format[0] != rate
            )
            self._format = (rate, learn)
        self._pending += pcm
        block_bytes = int(rate * 0.032) * 2
        events = []
        while len(self._pending) >= block_bytes:
            block, self._pending = (
                self._pending[:block_bytes],
                self._pending[block_bytes:],
            )
            samples = np.frombuffer(block, dtype="<i2").astype(np.float32) / 32768
            speech = float(self.vad(samples, rate).item()) >= 0.6
            if not self._window and not speech:
                continue
            self._window.append(samples)
            self._speech_blocks += int(speech)
            self._silence_blocks = 0 if speech else self._silence_blocks + 1
            # 1.6-second disjoint windows; close earlier after 384 ms silence.
            if len(self._window) >= 50 or self._silence_blocks >= 12:
                window_end = end - len(self._pending) / (2 * rate)
                events.append(self._finish_window(rate, window_end, learn))
        return events

    def flush(self, rate: int, end: float, learn: bool) -> list[dict]:
        return [self._finish_window(rate, end, learn)] if self._window else []

    def _finish_window(self, rate: int, end: float, learn: bool) -> dict:
        samples = np.concatenate(self._window)
        duration = len(samples) / rate
        speech_seconds = self._speech_blocks * 0.032
        speech_fraction = speech_seconds / duration
        rms = float(np.sqrt(np.mean(samples**2)))
        clipped = float(np.mean(np.abs(samples) >= 0.99))
        self._window = []
        self._speech_blocks = self._silence_blocks = 0
        timing = {
            "audio_start_seconds": round(end - duration, 3),
            "audio_end_seconds": round(end, 3),
            "speech_seconds": round(speech_seconds, 3),
            "speech_fraction": round(speech_fraction, 3),
            "sample_rate": rate,
            "learning_allowed": learn,
        }
        if (
            speech_seconds < 0.8
            or speech_fraction < 0.6
            or rms < 0.003
            or clipped > 0.03
        ):
            self.tracker.break_sequence()
            return {**timing, "action": "skipped", "reason": "insufficient_quality"}
        started = time.monotonic()
        embedding = embed(samples, rate)
        result = self.tracker.observe(
            embedding, speech_seconds=speech_seconds, end=end, learn=learn
        )
        return {
            **timing,
            **result,
            "embedding_ms": round((time.monotonic() - started) * 1000, 2),
        }

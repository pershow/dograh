"""Conservative online clustering of disjoint speech windows.

These are hypotheses about voice continuity, not identity or proof that someone
is addressing the agent. Thresholds need calibration on real telephone calls.
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class Voice:
    centroid: np.ndarray
    windows: int
    speech_seconds: float
    last_seen: float


class SpeakerTracker:
    MATCH = 0.55
    NEW = 0.40
    MARGIN = 0.08
    NOVEL_MATCH = 0.65
    NOVEL_MARGIN = 0.10
    MAX_VOICES = 8

    def __init__(self):
        self.voices: dict[str, Voice] = {}
        self.primary: str | None = None
        self.break_sequence()

    def break_sequence(self, *, clear_novel=True):
        self._candidate: str | None = None
        self._windows = 0
        self._seconds = 0.0
        self._last_end = 0.0
        if clear_novel:
            self._novel: Voice | None = None

    def observe(
        self, embedding: np.ndarray, *, speech_seconds: float, end: float, learn: bool
    ) -> dict:
        vector = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if vector.ndim != 1 or not np.isfinite(vector).all() or norm < 1e-6:
            self.break_sequence()
            return self._decision("uncertain", reason="invalid_embedding")
        vector = vector / norm
        scores = sorted(
            (
                (float(vector @ voice.centroid), key)
                for key, voice in self.voices.items()
            ),
            reverse=True,
        )
        score, speaker = scores[0] if scores else (None, None)
        details = {}
        if score is not None and self.NEW <= score < self.MATCH:
            # Telephone bandwidth can leave a different voice in the gray band
            # indefinitely. Keep a separate probationary reference: it must be
            # coherent across windows and better than every known reference.
            # Neither a single mismatch nor this reference can update primary.
            self.break_sequence(clear_novel=False)
            if not learn:
                self._novel = None
                return self._decision(
                    "uncertain", score=score, reason="learning_suspended"
                )
            novel_score = float(vector @ self._novel.centroid) if self._novel else None
            if (
                self._novel is None
                or end - self._novel.last_seen > 8
                or novel_score < self.NOVEL_MATCH
                or novel_score - score < self.NOVEL_MARGIN
            ):
                self._novel = Voice(vector, 1, speech_seconds, end)
            else:
                novel = self._novel
                updated = novel.centroid * novel.windows + vector
                novel.centroid = updated / np.linalg.norm(updated)
                novel.windows += 1
                novel.speech_seconds += speech_seconds
                novel.last_seen = end
            novel = self._novel
            if novel.windows < 3 or novel.speech_seconds < 3.0:
                return self._decision(
                    "uncertain",
                    score=score,
                    reason="novel_voice_pending",
                    candidate_windows=novel.windows,
                    candidate_similarity=round(novel_score, 4)
                    if novel_score is not None
                    else None,
                )
            if len(self.voices) >= self.MAX_VOICES:
                self.break_sequence()
                return self._decision(
                    "uncertain", score=score, reason="speaker_capacity"
                )
            details = {
                "reason": "consistent_novel_voice",
                "previous_best_similarity": round(score, 4),
            }
            speaker = f"speaker_{len(self.voices) + 1}"
            self.voices[speaker] = Voice(
                novel.centroid,
                novel.windows - 1,
                novel.speech_seconds - speech_seconds,
                end,
            )
            self._candidate = speaker
            self._windows = novel.windows - 1
            self._seconds = novel.speech_seconds - speech_seconds
            self._last_end = end
            score = novel_score
        self._novel = None
        if score is not None and (
            not details
            and score >= self.MATCH
            and len(scores) > 1
            and score - scores[1][0] < self.MARGIN
        ):
            self.break_sequence()
            return self._decision(
                "uncertain", score=score, reason="ambiguous_similarity"
            )

        if score is None or score < self.NEW:
            if not learn or len(self.voices) >= self.MAX_VOICES:
                self.break_sequence()
                return self._decision(
                    "uncertain",
                    score=score,
                    reason="learning_suspended" if not learn else "speaker_capacity",
                )
            speaker = f"speaker_{len(self.voices) + 1}"
            self.voices[speaker] = Voice(vector, 0, 0.0, end)

        if not learn:
            return self._decision(
                "primary_match" if speaker == self.primary else "other_speaker",
                speaker=speaker,
                score=score,
                reason="learning_suspended",
            )

        voice = self.voices[speaker]
        # Avoid gradual reference drift on marginal matches.
        if score is not None and score >= 0.65:
            updated = 0.9 * voice.centroid + 0.1 * vector
            voice.centroid = updated / np.linalg.norm(updated)
        voice.windows += 1
        voice.speech_seconds += speech_seconds
        voice.last_seen = end
        if self._candidate != speaker or end - self._last_end > 8:
            self.break_sequence()
            self._candidate = speaker
        self._windows += 1
        self._seconds += speech_seconds
        self._last_end = end

        action = "primary_match" if speaker == self.primary else "candidate"
        previous = self.primary
        if self.primary is None and self._windows >= 3 and self._seconds >= 3.0:
            self.primary = speaker
            action = "primary_established"
        elif (
            self.primary is not None
            and speaker != self.primary
            and self._windows >= 3
            and self._seconds >= 4.5
            and end - self.voices[self.primary].last_seen >= 4.0
        ):
            self.primary = speaker
            action = "primary_changed"
        return self._decision(
            action,
            speaker=speaker,
            score=score,
            previous_primary=previous if action == "primary_changed" else None,
            consecutive_windows=self._windows,
            consecutive_speech_seconds=round(self._seconds, 3),
            **details,
        )

    def _decision(self, action: str, *, score=None, **details) -> dict:
        return {
            "action": action,
            "primary_speaker": self.primary,
            "similarity": round(score, 4) if score is not None else None,
            **details,
        }

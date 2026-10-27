"""Count speech consistently for spaced languages and unspaced CJK text."""

import re

from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy


def speech_unit_count(text):
    # Preserve spaced-language tokens (including contractions); count each
    # CJK character separately and ignore standalone punctuation/noise.
    spaced = re.sub(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])", r" \1 ", text)
    return sum(
        any(character.isalnum() for character in token) for token in spaced.split()
    )


class SpeechUnitsUserTurnStartStrategy(MinWordsUserTurnStartStrategy):
    async def _handle_transcription(self, frame):
        threshold = self._min_words if self._bot_speaking else 1
        if speech_unit_count(frame.text) >= threshold:
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        await self.trigger_reset_aggregation()
        return ProcessFrameResult.CONTINUE

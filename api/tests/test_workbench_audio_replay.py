import io
import wave

import pytest

from api.schemas.agent_workbench import AudioReplay, SimulationCase
from api.services.agent_workbench.audio_replay import (
    audio_assertions,
    decode_wav,
    encode_wav,
    make_timeline,
)
from api.services.agent_workbench.errors import WorkbenchError


def test_wav_is_pcm_bounded_and_integrity_checked():
    assert decode_wav(encode_wav(b"\x01\x00" * 800, 8000)) == (b"\x01\x00" * 800, 8000)
    with pytest.raises(WorkbenchError):
        decode_wav(b"not a WAV")
    data = encode_wav(b"\x01\x00" * 800, 8000)
    with pytest.raises(WorkbenchError, match="不完整"):
        decode_wav(data[:-10])
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0" * 3200)
    with pytest.raises(WorkbenchError, match="单声道"):
        decode_wav(stream.getvalue())


def test_audio_timeline_rejects_rate_mismatch_overlap_and_overflow():
    spec = AudioReplay(
        channel="phone",
        clips=[{"asset_id": "a", "offset_ms": 0}, {"asset_id": "b", "offset_ms": 100}],
        duration_ms=1000,
    )
    with pytest.raises(WorkbenchError, match="16000|8000"):
        make_timeline(spec, [(b"\0" * 800, 16000)] * 2)
    with pytest.raises(WorkbenchError, match="重叠"):
        make_timeline(spec, [(b"\0" * 3200, 8000)] * 2)
    timeline, rate, receipts = make_timeline(spec, [(b"\x01\x00" * 400, 8000)] * 2)
    assert len(timeline) == 16000 and rate == 8000
    assert timeline[800:1600] == b"\0" * 800
    assert len(receipts) == 2


def test_audio_expected_transcripts_are_checks_not_injected_text():
    case = SimulationCase(
        name="Mandarin",
        user_messages=["你好", "需要帮助"],
        assertions=[{"kind": "contains", "value": "您好"}],
        audio={
            "clips": [
                {"asset_id": "a", "offset_ms": 0},
                {"asset_id": "b", "offset_ms": 1000},
            ]
        },
    )
    checks = audio_assertions(
        case, [{"role": "user", "text": "你好，我需要帮助。"}], {"input_coverage": 1}
    )
    assert all(row["passed"] for row in checks)
    checks = audio_assertions(
        case, [{"role": "user", "text": "需要帮助，然后你好"}], {"input_coverage": 0.5}
    )
    assert not any(row["passed"] for row in checks)

import asyncio
import base64
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.services.settings import TTSSettings
from pydantic import TypeAdapter, ValidationError
from websockets.exceptions import InvalidStatus
from websockets.protocol import State

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.model_connections import (
    _build_service,
    model_connection_catalog,
    split_service_configuration,
)
from api.services.configuration.options.qwen import (
    QWEN_REALTIME_URL,
    QWEN_STT_MODEL,
    QWEN_TTS_MODEL,
)
from api.services.configuration.registry import (
    QwenSTTConfiguration,
    QwenTTSConfiguration,
    STTConfig,
    TTSConfig,
)
from api.services.pipecat.qwen_stt import QwenSTTService
from api.services.pipecat.qwen_tts import QwenTTSService
from api.services.pipecat.service_factory import create_stt_service, create_tts_service


class FakeWebsocket:
    def __init__(self, *events):
        self.events = deque(events)
        self.sent = []
        self.state = State.OPEN
        self.close = AsyncMock(side_effect=self._close)

    async def _close(self):
        self.state = State.CLOSED

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if self.events:
            return json.dumps(self.events.popleft())
        await asyncio.Event().wait()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return await self.recv()


def test_qwen_catalog_and_saved_configuration_roundtrip():
    catalog = model_connection_catalog()["services"]
    for role, schema, union, model in (
        ("stt", QwenSTTConfiguration, STTConfig, QWEN_STT_MODEL),
        ("tts", QwenTTSConfiguration, TTSConfig, QWEN_TTS_MODEL),
    ):
        entry = catalog[role]["qwen"]
        assert entry["title"] == "Qwen (DashScope)"
        assert entry["credential_required"] == ["api_key"]
        assert entry["connection_fields"]["base_url"]["default"] == QWEN_REALTIME_URL
        assert "base_url" not in entry["settings_schema"]["properties"]
        config = schema(api_key=["first", "second"])
        provider, credentials, connection, settings = split_service_configuration(
            config
        )
        assert "api_key" not in settings
        assert settings["model"] == model
        assert (
            _build_service(role, provider, credentials, connection, settings) == config
        )
        assert isinstance(
            TypeAdapter(union).validate_python(config.model_dump()), schema
        )


@pytest.mark.parametrize("config", [QwenSTTConfiguration, QwenTTSConfiguration])
@pytest.mark.parametrize(
    "url",
    [
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "ws://dashscope.aliyuncs.com/api-ws/v1/realtime",
        QWEN_REALTIME_URL + "?model=other",
        QWEN_REALTIME_URL + "#fragment",
        "wss://user:secret@dashscope.aliyuncs.com/api-ws/v1/realtime",
    ],
)
def test_qwen_rejects_incompatible_connection_urls(config, url):
    with pytest.raises(ValidationError):
        config(api_key="key", base_url=url)


@pytest.mark.parametrize(
    "config,field,value",
    [
        (QwenSTTConfiguration, "model", "qwen3-asr-flash"),
        (QwenTTSConfiguration, "model", "qwen3-tts-flash"),
        (QwenSTTConfiguration, "language", "invalid"),
        (QwenTTSConfiguration, "language", "zh"),
        (QwenTTSConfiguration, "speed", 3),
    ],
)
def test_qwen_rejects_unsupported_settings(config, field, value):
    with pytest.raises(ValidationError):
        config(api_key="key", **{field: value})


@pytest.mark.parametrize("rate", [8000, 16000, 24000, 48000])
def test_factories_create_real_qwen_services(rate):
    config = SimpleNamespace(
        stt=QwenSTTConfiguration(api_key="key", language="zh"),
        tts=QwenTTSConfiguration(
            api_key="key", voice="Serena", language="Chinese", speed=1.2
        ),
    )
    audio = SimpleNamespace(
        transport_in_sample_rate=rate, transport_out_sample_rate=rate
    )
    stt = create_stt_service(config, audio)
    tts = create_tts_service(config, audio)
    assert isinstance(stt, QwenSTTService)
    assert stt._wire_sample_rate == (8000 if rate == 8000 else 16000)
    assert stt._settings.language == "zh"
    assert isinstance(tts, QwenTTSService)
    assert tts._settings.voice == "Serena"
    assert tts._settings.language == "Chinese"
    assert tts._init_sample_rate == rate
    assert tts._speed == 1.2
    stt._settings.validate_complete()
    tts._settings.validate_complete()


@pytest.mark.parametrize(
    "rate,language", [(8000, "zh"), (16000, "auto"), (24000, "yue")]
)
@pytest.mark.asyncio
async def test_stt_handshake_audio_and_transcripts(rate, language):
    ws = FakeWebsocket({"type": "session.created"}, {"type": "session.updated"})
    service = QwenSTTService(api_key="key", sample_rate=rate, language=language)
    service._sample_rate = rate
    service._websocket_connect = AsyncMock(return_value=ws)
    service.push_frame = AsyncMock()
    service.emit_stt_usage_metrics = AsyncMock()
    await service._connect_websocket()
    kwargs = service._websocket_connect.call_args.kwargs
    assert kwargs["additional_headers"] == {"Authorization": "Bearer key"}
    url = service._websocket_connect.call_args.args[0]
    assert parse_qs(urlsplit(url).query) == {"model": [QWEN_STT_MODEL]}
    session = ws.sent[0]["session"]
    assert session["sample_rate"] == (8000 if rate == 8000 else 16000)
    assert session["input_audio_format"] == "pcm"
    assert session["input_audio_transcription"] == (
        {} if language == "auto" else {"language": language}
    )
    assert session["turn_detection"]["type"] == "server_vad"

    # Resampling must change the bytes, not just relabel a higher-rate stream.
    service._resampler.resample = AsyncMock(return_value=b"\x01\x00" * 160)
    assert [frame async for frame in service.run_stt(b"input")] == [None]
    service._resampler.resample.assert_awaited_once_with(
        b"input", rate, session["sample_rate"]
    )
    assert base64.b64decode(ws.sent[1]["audio"]) == b"\x01\x00" * 160
    assert ws.sent[0]["event_id"] != ws.sent[1]["event_id"]
    ws.events.extend(
        [
            {
                "type": "conversation.item.input_audio_transcription.text",
                "text": "你好",
                "stash": "世界",
            },
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "你好世界。",
            },
            {"type": "session.finished"},
        ]
    )
    await service._receive_messages()
    frames = [call.args[0] for call in service.push_frame.call_args_list]
    assert isinstance(frames[0], InterimTranscriptionFrame)
    assert frames[0].text == "你好世界"
    assert isinstance(frames[1], TranscriptionFrame)
    assert frames[1].text == "你好世界。"
    assert service._session_finished.is_set()
    service.emit_stt_usage_metrics.assert_awaited_once()
    await service._disconnect_websocket()
    ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_stt_rejected_session_closes_socket():
    ws = FakeWebsocket(
        {
            "type": "error",
            "error": {"code": "InvalidApiKey", "message": "Invalid API key"},
        }
    )
    service = QwenSTTService(api_key="key")
    service._websocket_connect = AsyncMock(return_value=ws)
    with pytest.raises(ValueError, match="InvalidApiKey"):
        await service._connect_websocket()
    ws.close.assert_awaited_once()
    assert service._websocket is None


@pytest.mark.asyncio
async def test_stt_failed_transcription_reports_error():
    service = QwenSTTService(api_key="key")
    service._websocket = FakeWebsocket(
        {
            "type": "conversation.item.input_audio_transcription.failed",
            "error": {"code": "InvalidInput", "message": "Bad audio"},
        }
    )
    with pytest.raises(ValueError, match="InvalidInput"):
        await service._receive_messages()


@pytest.mark.parametrize("rate", [8000, 16000, 24000, 48000])
@pytest.mark.asyncio
async def test_tts_streams_pcm_and_reuses_configured_session(rate):
    audio = b"\x01\x00" * 160
    ws = FakeWebsocket(
        {"type": "session.created"},
        {"type": "session.updated"},
        {"type": "response.audio.delta", "delta": base64.b64encode(audio).decode()},
        {"type": "response.audio.done"},
        {"type": "response.done", "response": {"status": "completed"}},
        {"type": "response.audio.delta", "delta": base64.b64encode(audio).decode()},
        {"type": "response.done"},
    )
    service = QwenTTSService(
        api_key="key", sample_rate=rate, voice="Serena", language="Chinese", speed=1.2
    )
    service._sample_rate = rate
    with patch(
        "api.services.pipecat.qwen_tts.connect", new_callable=AsyncMock, return_value=ws
    ) as connect:
        first = [frame async for frame in service.run_tts("你好。", "first")]
        second = [frame async for frame in service.run_tts("再见。", "second")]
    connect.assert_awaited_once()
    assert len(first) == len(second) == 1
    assert isinstance(first[0], TTSAudioRawFrame)
    assert first[0].audio == audio
    assert first[0].sample_rate == rate
    assert first[0].context_id == "first"
    assert second[0].context_id == "second"
    assert ws.sent[0]["session"] == {
        "voice": "Serena",
        "language_type": "Chinese",
        "mode": "commit",
        "response_format": "pcm",
        "sample_rate": rate,
        "speech_rate": 1.2,
    }
    assert [event["type"] for event in ws.sent] == [
        "session.update",
        "input_text_buffer.append",
        "input_text_buffer.commit",
        "input_text_buffer.append",
        "input_text_buffer.commit",
    ]
    await service._disconnect()


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "error",
            "error": {"code": "InvalidApiKey", "message": "Invalid API key"},
        },
        {
            "type": "response.done",
            "response": {"status": "failed", "status_details": "Synthesis failed"},
        },
        {"type": "session.finished"},
    ],
)
@pytest.mark.asyncio
async def test_tts_errors_close_connection_and_emit_error_frame(event):
    ws = FakeWebsocket({"type": "session.updated"}, event)
    service = QwenTTSService(api_key="key")
    service._sample_rate = 24000
    with patch(
        "api.services.pipecat.qwen_tts.connect", new_callable=AsyncMock, return_value=ws
    ):
        frames = [frame async for frame in service.run_tts("你好", "context")]
    assert len(frames) == 1
    assert isinstance(frames[0], ErrorFrame)
    assert service._websocket is None
    ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_tts_cancellation_closes_pending_generation():
    ws = FakeWebsocket({"type": "session.updated"})
    service = QwenTTSService(api_key="key")
    service._sample_rate = 24000
    with patch(
        "api.services.pipecat.qwen_tts.connect", new_callable=AsyncMock, return_value=ws
    ):
        generator = service.run_tts("你好", "interrupted")
        task = asyncio.create_task(anext(generator))
        while len(ws.sent) < 3:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    ws.close.assert_awaited_once()
    assert service._websocket is None


@pytest.mark.asyncio
async def test_tts_voice_update_discards_old_session():
    service = QwenTTSService(api_key="key")
    ws = FakeWebsocket()
    service._websocket = ws
    await service._update_settings(TTSSettings(voice="Serena"))
    assert service._settings.voice == "Serena"
    assert service._websocket is None
    ws.close.assert_awaited_once()


def test_qwen_validation_checks_each_key_at_configured_region():
    config = QwenSTTConfiguration(
        api_key=["first", "second"],
        base_url="wss://workspace.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime",
    )
    with patch(
        "api.services.configuration.check_validity.websocket_connect"
    ) as connect:
        connect.return_value.__enter__.return_value.recv.return_value = json.dumps(
            {"type": "session.created"}
        )
        UserConfigurationValidator().validate_connection(config)
    assert connect.call_count == 2
    assert [
        call.kwargs["additional_headers"]["Authorization"]
        for call in connect.call_args_list
    ] == ["Bearer first", "Bearer second"]
    assert all(
        call.args[0].startswith(config.base_url + "?model=")
        for call in connect.call_args_list
    )


@pytest.mark.parametrize("status", [401, 403])
def test_qwen_validation_reports_region_or_key_errors(status):
    response = MagicMock(status_code=status)
    with (
        patch(
            "api.services.configuration.check_validity.websocket_connect",
            side_effect=InvalidStatus(response),
        ),
        pytest.raises(ValueError, match="selected DashScope region"),
    ):
        UserConfigurationValidator().validate_connection(
            QwenSTTConfiguration(api_key="bad")
        )


def test_qwen_validation_does_not_accept_network_failures():
    with (
        patch(
            "api.services.configuration.check_validity.websocket_connect",
            side_effect=TimeoutError,
        ),
        pytest.raises(ValueError, match="network connectivity"),
    ):
        UserConfigurationValidator().validate_connection(
            QwenTTSConfiguration(api_key="key")
        )


def test_qwen_validation_applies_hosted_url_protection():
    config = QwenTTSConfiguration(
        api_key="key", base_url="wss://127.0.0.1/api-ws/v1/realtime"
    )
    with (
        patch("api.utils.url_security.DEPLOYMENT_MODE", "saas"),
        patch("api.services.configuration.check_validity.websocket_connect") as connect,
        pytest.raises(ValueError, match="public IP"),
    ):
        UserConfigurationValidator().validate_connection(config)
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_tts_pipeline_delivers_audio_text_and_stop_frames():
    from pipecat.frames.frames import (
        TTSSpeakFrame,
        TTSStartedFrame,
        TTSStoppedFrame,
        TTSTextFrame,
    )
    from pipecat.tests.utils import run_test

    audio = b"\x01\x00" * 2400
    ws = FakeWebsocket(
        {"type": "session.updated"},
        {"type": "response.audio.delta", "delta": base64.b64encode(audio).decode()},
        {"type": "response.done"},
    )
    service = QwenTTSService(api_key="key")
    with patch(
        "api.services.pipecat.qwen_tts.connect", new_callable=AsyncMock, return_value=ws
    ):
        down, up = await asyncio.wait_for(
            run_test(service, frames_to_send=[TTSSpeakFrame("你好。")]), timeout=10
        )
    assert not [frame for frame in down + up if isinstance(frame, ErrorFrame)]
    assert (
        b"".join(frame.audio for frame in down if isinstance(frame, TTSAudioRawFrame))
        == audio
    )
    assert [frame.text for frame in down if isinstance(frame, TTSTextFrame)] == [
        "你好。"
    ]
    assert sum(isinstance(frame, TTSStartedFrame) for frame in down) == 1
    assert sum(isinstance(frame, TTSStoppedFrame) for frame in down) == 1
    assert service._websocket is None
    ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_stt_pipeline_finishes_session_and_releases_receiver():
    from pipecat.frames.frames import InputAudioRawFrame
    from pipecat.tests.utils import SleepFrame, run_test

    class StreamingSocket(FakeWebsocket):
        def __init__(self):
            super().__init__()
            self.incoming = asyncio.Queue()

        async def recv(self):
            return json.dumps(await self.incoming.get())

        async def __anext__(self):
            return await self.recv()

        async def send(self, message):
            await super().send(message)
            event_type = self.sent[-1]["type"]
            if event_type == "session.update":
                await self.incoming.put({"type": "session.updated"})
            elif event_type == "input_audio_buffer.append":
                await self.incoming.put(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "你好。",
                    }
                )
            elif event_type == "session.finish":
                await self.incoming.put({"type": "session.finished"})

    ws = StreamingSocket()
    service = QwenSTTService(api_key="key")
    service._websocket_connect = AsyncMock(return_value=ws)
    down, up = await asyncio.wait_for(
        run_test(
            service,
            frames_to_send=[
                InputAudioRawFrame(b"\x01\x00" * 1600, 16000, 1),
                SleepFrame(0.05),
            ],
        ),
        timeout=10,
    )
    assert not [frame for frame in down + up if isinstance(frame, ErrorFrame)]
    assert [frame.text for frame in down if isinstance(frame, TranscriptionFrame)] == [
        "你好。"
    ]
    assert ws.sent[-1]["type"] == "session.finish"
    assert service._receive_task is None
    assert service._websocket is None
    ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_tts_pipeline_interruption_reconnects_for_next_reply():
    from pipecat.frames.frames import InterruptionFrame, TTSSpeakFrame, TTSTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    # The first response is stalled; interruption must release its receive wait
    # and create a fresh session for the second response.
    interrupted = FakeWebsocket({"type": "session.updated"})
    audio = b"\x01\x00" * 2400
    resumed = FakeWebsocket(
        {"type": "session.updated"},
        {"type": "response.audio.delta", "delta": base64.b64encode(audio).decode()},
        {"type": "response.done"},
    )
    service = QwenTTSService(api_key="key")
    with patch(
        "api.services.pipecat.qwen_tts.connect",
        new_callable=AsyncMock,
        side_effect=[interrupted, resumed],
    ):
        down, up = await asyncio.wait_for(
            run_test(
                service,
                frames_to_send=[
                    TTSSpeakFrame("第一句。"),
                    SleepFrame(0.05),
                    InterruptionFrame(),
                    SleepFrame(0.05),
                    TTSSpeakFrame("第二句。"),
                ],
            ),
            timeout=10,
        )
    assert not [frame for frame in down + up if isinstance(frame, ErrorFrame)]
    assert [frame.text for frame in down if isinstance(frame, TTSTextFrame)] == [
        "第二句。"
    ]
    assert (
        b"".join(frame.audio for frame in down if isinstance(frame, TTSAudioRawFrame))
        == audio
    )
    interrupted.close.assert_awaited_once()
    resumed.close.assert_awaited_once()

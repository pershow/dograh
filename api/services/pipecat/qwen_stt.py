"""Streaming Qwen ASR over the DashScope realtime WebSocket API."""

import asyncio
import base64
import json

from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from api.services.configuration.options.qwen import (
    QWEN_REALTIME_URL,
    QWEN_STT_MODEL,
    qwen_realtime_url,
)
from api.services.pipecat.qwen import (
    check_qwen_error,
    configure_qwen_session,
    qwen_event,
)
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    EndFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import WebsocketSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601
from pipecat.utils.tracing.service_decorators import traced_stt


class QwenSTTService(WebsocketSTTService):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = QWEN_REALTIME_URL,
        model: str = QWEN_STT_MODEL,
        language: str = "auto",
        sample_rate: int = 16000,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            settings=STTSettings(model=model, language=language),
            **kwargs,
        )
        self._api_key = api_key
        self._base_url = base_url
        self._wire_sample_rate = 8000 if sample_rate == 8000 else 16000
        self._resampler = create_stream_resampler()
        self._receive_task = None
        self._session_finished = asyncio.Event()

    def can_generate_metrics(self) -> bool:
        return True

    def language_to_service_language(self, language: Language) -> str:
        return language.value

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        await self._connect()

    async def _update_settings(self, delta):
        changed = await super()._update_settings(delta)
        if changed and self._websocket:
            await self._disconnect()
            await self._connect()
        return changed

    async def stop(self, frame: EndFrame):
        # Drain the final utterance before stopping the receiver.
        if self._websocket and self._websocket.state is State.OPEN:
            self._disconnecting = True
            try:
                await self._websocket.send(qwen_event("session.finish"))
                await asyncio.wait_for(self._session_finished.wait(), timeout=2)
            except (TimeoutError, ConnectionError, ConnectionClosed):
                pass
            finally:
                await self._disconnect()
        await super().stop(frame)

    async def _connect(self):
        await super()._connect()
        await self._connect_websocket()
        if self._websocket and self._receive_task is None:
            self._receive_task = self.create_task(
                self._receive_task_handler(self._report_error)
            )

    async def _disconnect(self):
        await super()._disconnect()
        if self._receive_task:
            await self.cancel_task(self._receive_task, timeout=1.0)
            self._receive_task = None
        await self._disconnect_websocket()

    async def _connect_websocket(self):
        if self._websocket and self._websocket.state is State.OPEN:
            return
        self._session_finished.clear()
        try:
            self._websocket = await self._websocket_connect(
                qwen_realtime_url(self._base_url, self._settings.model),
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
                open_timeout=10,
            )
            transcription = {}
            if self._settings.language != "auto":
                transcription["language"] = self._settings.language
            await configure_qwen_session(
                self._websocket,
                {
                    "input_audio_format": "pcm",
                    "sample_rate": self._wire_sample_rate,
                    "input_audio_transcription": transcription,
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.0,
                        "silence_duration_ms": 400,
                    },
                },
            )
            await self._call_event_handler("on_connected")
        except BaseException:
            await self._disconnect_websocket()
            raise

    async def _disconnect_websocket(self):
        websocket, self._websocket = self._websocket, None
        if websocket:
            await websocket.close()
            await self._call_event_handler("on_disconnected")

    async def run_stt(self, audio: bytes):
        if self._disconnecting:
            yield None
            return
        audio = await self._resampler.resample(
            audio, self.sample_rate, self._wire_sample_rate
        )
        if audio:
            await self.send_with_retry(
                qwen_event(
                    "input_audio_buffer.append",
                    audio=base64.b64encode(audio).decode("ascii"),
                ),
                self._report_error,
            )
        yield None

    async def _receive_messages(self):
        async for message in self._websocket:
            event = json.loads(message)
            check_qwen_error(event)
            event_type = event.get("type")
            if event_type == "session.finished":
                self._session_finished.set()
                return
            if event_type == "conversation.item.input_audio_transcription.text":
                text = (event.get("text", "") + event.get("stash", "")).strip()
                if text:
                    await self.push_frame(
                        InterimTranscriptionFrame(
                            text, self._user_id, time_now_iso8601(), result=event
                        )
                    )
            elif event_type == "conversation.item.input_audio_transcription.completed":
                text = event.get("transcript", "").strip()
                if text:
                    await self.emit_stt_usage_metrics()
                    await self.push_frame(
                        TranscriptionFrame(
                            text, self._user_id, time_now_iso8601(), result=event
                        )
                    )
                    await self._handle_transcription_trace(text, True)

    @traced_stt
    async def _handle_transcription_trace(self, transcript: str, is_final: bool):
        pass

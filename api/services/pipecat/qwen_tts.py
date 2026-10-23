"""Qwen streaming TTS with a reusable, interruptible DashScope session."""

import asyncio
import base64

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException
from websockets.protocol import State

from api.services.configuration.options.qwen import (
    QWEN_REALTIME_URL,
    QWEN_TTS_MODEL,
    qwen_realtime_url,
)
from api.services.pipecat.qwen import (
    configure_qwen_session,
    qwen_event,
    receive_qwen_event,
)
from pipecat.frames.frames import CancelFrame, EndFrame, ErrorFrame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService


class QwenTTSService(TTSService):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = QWEN_REALTIME_URL,
        model: str = QWEN_TTS_MODEL,
        voice: str = "Cherry",
        language: str = "Auto",
        speed: float = 1.0,
        sample_rate: int = 24000,
        **kwargs,
    ):
        # Other transport rates are resampled downstream from 24 kHz PCM.
        sample_rate = (
            sample_rate if sample_rate in (8000, 16000, 24000, 48000) else 24000
        )
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            settings=TTSSettings(model=model, voice=voice, language=language),
            **kwargs,
        )
        self._api_key = api_key
        self._base_url = base_url
        self._speed = speed
        self._websocket = None

    def can_generate_metrics(self) -> bool:
        return True

    async def _connect(self):
        if self._websocket and self._websocket.state is State.OPEN:
            return
        self._websocket = await connect(
            qwen_realtime_url(self._base_url, self._settings.model),
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=10,
            close_timeout=1,
        )
        await configure_qwen_session(
            self._websocket,
            {
                "voice": self._settings.voice,
                "language_type": self._settings.language,
                "mode": "commit",
                "response_format": "pcm",
                "sample_rate": self.sample_rate,
                "speech_rate": self._speed,
            },
        )
        await self._call_event_handler("on_connected")

    async def _disconnect(self):
        websocket, self._websocket = self._websocket, None
        if websocket:
            await websocket.close()
            await self._call_event_handler("on_disconnected")

    async def _update_settings(self, delta):
        changed = await super()._update_settings(delta)
        if changed:
            await self._disconnect()
        return changed

    async def _handle_interruption(self, frame, direction):
        await super()._handle_interruption(frame, direction)
        # Qwen TTS has no response.cancel event. Closing discards any remaining
        # audio so it cannot leak into the next reply's audio context.
        await self._disconnect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def cleanup(self):
        await super().cleanup()
        await self._disconnect()

    async def run_tts(self, text: str, context_id: str):
        try:
            await self._connect()
            await self._websocket.send(
                qwen_event("input_text_buffer.append", text=text)
            )
            await self._websocket.send(qwen_event("input_text_buffer.commit"))
            await self.start_tts_usage_metrics(text)
            while True:
                event = await receive_qwen_event(self._websocket)
                if event.get("type") == "response.audio.delta":
                    audio = base64.b64decode(event["delta"], validate=True)
                    if audio:
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(
                            audio, self.sample_rate, 1, context_id=context_id
                        )
                elif event.get("type") == "response.done":
                    response = event.get("response") or {}
                    if response.get("status", "completed") != "completed":
                        raise ValueError(
                            f"Qwen TTS response failed: {response.get('status_details')}"
                        )
                    return
                elif event.get("type") == "session.finished":
                    raise ConnectionError(
                        "Qwen TTS session ended before synthesis completed"
                    )
        except (asyncio.CancelledError, GeneratorExit):
            await self._disconnect()
            raise
        except (OSError, ValueError, WebSocketException) as exc:
            await self._disconnect()
            yield ErrorFrame(error=f"Qwen TTS failed: {exc}", exception=exc)

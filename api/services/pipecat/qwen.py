"""Qwen realtime wire helpers. Speech uses DashScope, not the chat API."""

import asyncio
import json
import uuid


def qwen_event(event_type: str, **payload) -> str:
    return json.dumps({"event_id": uuid.uuid4().hex, "type": event_type, **payload})


def check_qwen_error(event: dict) -> None:
    if event.get("type") in (
        "error",
        "conversation.item.input_audio_transcription.failed",
    ):
        error = event.get("error") or {}
        raise ValueError(
            f"Qwen [{error.get('code', 'unknown')}]: "
            f"{error.get('message', 'Speech request failed')}"
        )


async def receive_qwen_event(websocket, *, timeout: float = 30) -> dict:
    event = json.loads(await asyncio.wait_for(websocket.recv(), timeout=timeout))
    check_qwen_error(event)
    return event


async def configure_qwen_session(websocket, session: dict) -> None:
    await websocket.send(qwen_event("session.update", session=session))
    # Do not send audio/text until the service accepts the settings. Bound the
    # whole handshake, including unexpected events, rather than each read only.
    async with asyncio.timeout(10):
        while True:
            event = await receive_qwen_event(websocket)
            if event.get("type") == "session.updated":
                return

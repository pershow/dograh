"""Batch audio transcription; never route files through realtime STT adapters."""

import re
from pathlib import Path

import httpx
from fastapi import HTTPException

from api.constants import DEPLOYMENT_MODE
from api.services.configuration.ai_model_configuration import (
    get_resolved_ai_model_configuration,
)
from api.services.mps_policy import check_model_service
from api.utils.url_security import validate_user_configured_service_url

MAX_AUDIO_BYTES = 5 * 1024 * 1024
AUDIO_EXTENSIONS = {".wav", ".mp3", ".mp4", ".m4a", ".ogg", ".flac", ".webm", ".aac"}


def failure(code, message, status=422):
    return HTTPException(status, detail={"code": code, "message": message})


async def resolve_file_transcription(organization_id):
    resolved = await get_resolved_ai_model_configuration(
        organization_id=organization_id
    )
    stt = resolved.effective.stt
    if not stt or not stt.api_key:
        raise failure(
            "file_transcription_missing",
            "Configure STT in your organization default Models configuration, or enter the recording transcript manually.",
        )
    check_model_service("stt", stt)
    provider = getattr(stt.provider, "value", stt.provider)
    if provider not in {"elevenlabs", "dograh"}:
        raise failure(
            "file_transcription_unsupported",
            f"File transcription is not supported for {provider}. Select an ElevenLabs STT connection or enter the transcript manually.",
        )
    return stt, resolved


def metadata(stt, resolved):
    snapshot = resolved.snapshot["services"]["stt"]
    provider = getattr(stt.provider, "value", stt.provider)
    return {
        "provider": provider,
        "model": getattr(stt, "file_transcription_model", "scribe_v2")
        if provider == "elevenlabs"
        else stt.model,
        "configured_stt_model": stt.model,
        "connection_uuid": snapshot["provider_connection_uuid"],
        "connection_revision": snapshot["connection_revision"],
        "provenance": resolved.provenance,
    }


async def capabilities(organization_id):
    try:
        stt, resolved = await resolve_file_transcription(organization_id)
        return {
            "available": True,
            "max_bytes": MAX_AUDIO_BYTES,
            **metadata(stt, resolved),
        }
    except HTTPException as error:
        return {"available": False, "max_bytes": MAX_AUDIO_BYTES, "issue": error.detail}


async def transcribe_file(organization_id, created_by, upload, language=None):
    filename = Path((upload.filename or "audio.wav").replace("\\", "/")).name
    if Path(filename).suffix.lower() not in AUDIO_EXTENSIONS:
        raise failure(
            "audio_format_unsupported",
            "Upload WAV, MP3, MP4, M4A, OGG, FLAC, WEBM or AAC audio.",
            415,
        )
    data = await upload.read(MAX_AUDIO_BYTES + 1)
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise failure(
            "audio_size_invalid", "Audio must be between 1 byte and 5 MiB.", 413
        )
    stt, resolved = await resolve_file_transcription(organization_id)
    language = language or getattr(stt, "language", "auto")
    if language in {"auto", "multi"}:
        language = None
    elif not re.fullmatch(r"[a-z]{2,3}", language):
        raise failure(
            "audio_language_invalid",
            "Choose an ISO language code or automatic language detection.",
        )
    info = metadata(stt, resolved)
    if info["provider"] == "dograh":
        # Backwards compatibility only when the user explicitly selected managed STT.
        from api.services.mps_service_key_client import mps_service_key_client

        auth = (
            {"created_by": str(created_by)}
            if DEPLOYMENT_MODE == "oss"
            else {"organization_id": organization_id}
        )
        result = await mps_service_key_client.transcribe_audio(
            audio_data=data,
            filename=filename,
            content_type=upload.content_type or "application/octet-stream",
            language=language or "multi",
            **auth,
        )
        return {**result, "transcription_configuration": info}
    base = stt.base_url.rstrip("/")
    validate_user_configured_service_url(base, field_name="base_url")
    url = base + ("/speech-to-text" if base.endswith("/v1") else "/v1/speech-to-text")
    fields = {
        "model_id": info["model"],
        "tag_audio_events": "false",
        "diarize": "false",
    }
    if language:
        fields["language_code"] = language
    try:
        # No redirects: do not forward a connection's credentials to another host.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=15), follow_redirects=False
        ) as client:
            response = await client.post(
                url,
                headers={"xi-api-key": stt.api_key},
                data=fields,
                files={
                    "file": (
                        filename,
                        data,
                        upload.content_type or "application/octet-stream",
                    )
                },
            )
        if response.status_code in {401, 403}:
            raise failure(
                "transcription_credentials_rejected",
                "The STT provider rejected the connection credentials. Check Providers or enter the transcript manually.",
                502,
            )
        if response.status_code == 429:
            raise failure(
                "transcription_rate_limited",
                "The STT provider is busy or has insufficient quota. Retry later or enter the transcript manually.",
                503,
            )
        if response.status_code != 200:
            raise failure(
                "transcription_provider_error",
                "The STT provider could not transcribe this file. Check the audio format and configured file model, or enter the transcript manually.",
                502,
            )
        result = response.json()
    except httpx.TimeoutException as error:
        raise failure(
            "transcription_timeout",
            "File transcription timed out. Retry or enter the transcript manually.",
            504,
        ) from error
    except (httpx.HTTPError, ValueError) as error:
        raise failure(
            "transcription_provider_error",
            "Unable to read a transcription from the configured STT provider. Retry or enter the transcript manually.",
            502,
        ) from error
    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise failure(
            "transcription_empty",
            "No speech was recognized. Enter the transcript manually or try another recording.",
        )
    return {
        "transcript": text.strip(),
        "language": result.get("language_code"),
        "transcription_configuration": info,
    }

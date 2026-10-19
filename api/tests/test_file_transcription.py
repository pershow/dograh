"""Provider file API contract, configuration reuse and safe failure surfaces."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, UploadFile

from api import constants
from api.services.transcription import files


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(constants, "MPS_ENABLED", False)
    stt = SimpleNamespace(
        provider="elevenlabs",
        api_key="test-secret",
        model="scribe_v2_realtime",
        file_transcription_model="scribe_v2",
        base_url="https://api.elevenlabs.io",
        language="zh",
    )
    resolved = SimpleNamespace(
        effective=SimpleNamespace(stt=stt),
        snapshot={
            "services": {
                "stt": {
                    "provider_connection_uuid": "scoped-connection",
                    "connection_revision": 2,
                }
            }
        },
        provenance=[{"source": "organization"}],
    )
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(files, "get_resolved_ai_model_configuration", resolver)
    return stt, resolver


def upload(data=b"test-audio", name="recording.wav"):
    return UploadFile(io.BytesIO(data), filename=name)


async def test_batch_api_reuses_connection_and_explicit_file_model(
    configured, monkeypatch
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"text": "首访咨询一百二十元", "language_code": "zho"}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        files.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    result = await files.transcribe_file(7, "u", upload(), "multi")
    configured[1].assert_awaited_once_with(organization_id=7)
    request = requests[0]
    assert str(request.url) == "https://api.elevenlabs.io/v1/speech-to-text"
    assert request.headers["xi-api-key"] == "test-secret"
    assert (
        b"scribe_v2\r\n" in request.content
        and b"scribe_v2_realtime" not in request.content
    )
    assert b"language_code" not in request.content
    assert result["transcript"] == "首访咨询一百二十元" and "test-secret" not in str(
        result
    )
    assert (
        result["transcription_configuration"]["configured_stt_model"]
        == "scribe_v2_realtime"
    )


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "transcription_credentials_rejected"),
        (429, "transcription_rate_limited"),
        (500, "transcription_provider_error"),
        (307, "transcription_provider_error"),
    ],
)
async def test_upstream_errors_do_not_expose_body_or_redirect(
    configured, monkeypatch, status, expected
):
    original = httpx.AsyncClient

    def response(request):
        return httpx.Response(
            status,
            text="sensitive upstream test-secret",
            headers={"location": "https://unrelated.example"},
        )

    monkeypatch.setattr(
        files.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(response), **kw),
    )
    with pytest.raises(HTTPException) as error:
        await files.transcribe_file(7, "u", upload(), "zh")
    assert error.value.detail["code"] == expected and "test-secret" not in str(
        error.value.detail
    )


async def test_unsupported_missing_and_managed_do_not_fallback(configured):
    stt, resolver = configured
    stt.provider = "xunfei"
    cap = await files.capabilities(7)
    assert (
        not cap["available"]
        and cap["issue"]["code"] == "file_transcription_unsupported"
    )
    stt.provider = "dograh"
    with pytest.raises(HTTPException) as error:
        await files.transcribe_file(7, "u", upload())
    assert error.value.detail["code"] == "mps_disabled"
    resolver.return_value.effective.stt = None
    cap = await files.capabilities(7)
    assert cap["issue"]["code"] == "file_transcription_missing"


@pytest.mark.parametrize(
    "data,name,status",
    [
        (b"", "a.wav", 413),
        (b"a" * (files.MAX_AUDIO_BYTES + 1), "a.wav", 413),
        (b"data", "file.txt", 415),
    ],
)
async def test_rejects_invalid_file_before_model_lookup(configured, data, name, status):
    with pytest.raises(HTTPException) as error:
        await files.transcribe_file(7, "u", upload(data, name))
    assert error.value.status_code == status
    configured[1].assert_not_awaited()


async def test_empty_transcript_is_an_error(configured, monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        files.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"text": " "})
            ),
            **kw,
        ),
    )
    with pytest.raises(HTTPException) as error:
        await files.transcribe_file(7, "u", upload())
    assert error.value.detail["code"] == "transcription_empty"

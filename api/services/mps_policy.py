"""Deployment-level boundary for Dograh managed services and BYOK endpoints."""

from urllib.parse import urlsplit

from fastapi import HTTPException

from api import constants


class MPSDisabledError(HTTPException):
    def __init__(self, *, role=None, provider=None):
        detail = {
            "code": "mps_disabled",
            "message": "Dograh managed services are disabled. Select a BYOK model connection.",
        }
        if role:
            detail["role"] = role
        if provider:
            detail["provider"] = str(provider)
        super().__init__(status_code=409, detail=detail)


def require_mps_enabled():
    if not constants.MPS_ENABLED:
        raise MPSDisabledError()


def endpoint_host(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        return (
            urlsplit(value if "://" in value else "//" + value).hostname or ""
        ).lower().rstrip(".") or None
    except ValueError:
        return None


def uses_mps(provider, *endpoints):
    provider = getattr(provider, "value", provider)
    hosts = {"services.dograh.com", endpoint_host(constants.MPS_API_URL)} - {None}
    return str(provider).lower() == "dograh" or any(
        endpoint_host(url) in hosts for url in endpoints
    )


def check_model_service(role, service=None, *, provider=None, **settings):
    if constants.MPS_ENABLED:
        return
    if service is not None:
        settings = (
            service
            if isinstance(service, dict)
            else service.model_dump()
            if hasattr(service, "model_dump")
            else vars(service)
        )
        provider = settings.get("provider")
    if uses_mps(
        provider,
        *(
            settings.get(key)
            for key in ("base_url", "endpoint", "url", "websocket_url")
        ),
    ):
        raise MPSDisabledError(role=role, provider=provider)


def check_effective_models(config):
    for role in ("llm", "stt", "tts", "realtime", "embeddings"):
        service = getattr(config, role, None)
        if service is not None:
            check_model_service(role, service)
    fallback = getattr(config, "llm_fallback", None)
    for index, rule in enumerate(fallback.rules if fallback else []):
        check_model_service(f"fallback_{index}", rule.target)


def check_definition_models(workflow_json, configurations):
    """Legacy explicit QA/voicemail models are outside the main catalog binding."""
    for node in (workflow_json or {}).get("nodes", []):
        data = node.get("data", {})
        if data.get("qa_use_workflow_llm") is False:
            check_model_service(
                "qa:" + str(node.get("id", "")),
                provider=data.get("qa_provider"),
                base_url=data.get("qa_base_url"),
                endpoint=data.get("qa_endpoint"),
            )
    voicemail = (configurations or {}).get("voicemail_detection") or {}
    if voicemail.get("enabled") and voicemail.get("use_workflow_llm") is False:
        check_model_service("voicemail", voicemail)

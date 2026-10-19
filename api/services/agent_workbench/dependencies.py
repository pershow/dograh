"""Resolve the actual scoped catalog binding before work and expose safe metadata."""

from fastapi import HTTPException

from api.services.configuration.model_connections import resolve_model_configuration
from api.services.mps_policy import (
    check_definition_models,
    check_effective_models,
    endpoint_host,
)


async def prepare_models(org, target, purpose):
    configurations = target.get("workflow_configurations") or {}
    check_definition_models(target.get("workflow_json"), configurations)
    resolved = await resolve_model_configuration(
        org, workflow_override=configurations.get("model_configuration_override")
    )
    check_effective_models(resolved.effective)
    required = ("llm", "stt", "tts") if purpose == "voice" else ("llm",)
    missing = [
        role for role in required if getattr(resolved.effective, role, None) is None
    ]
    if missing:
        raise HTTPException(
            422,
            detail={
                "code": "model_configuration_missing",
                "roles": missing,
                "message": "Configure "
                + ", ".join(missing)
                + " in Dograh Models before starting "
                + purpose,
            },
        )
    return resolved


def model_report(resolved, purpose):
    services = dict(resolved.snapshot["services"])
    for i, rule in enumerate(
        (resolved.snapshot.get("llm_fallback") or {}).get("rules", [])
    ):
        services[f"fallback_{i}"] = rule["target"]
    return {
        "ready": True,
        "purpose": purpose,
        "issues": [],
        "provenance": resolved.provenance,
        "services": [
            {
                "role": role,
                "provider": service["provider"],
                "model": service["settings"].get("model"),
                "connection_uuid": service["provider_connection_uuid"],
                "connection_revision": service["connection_revision"],
                "endpoint_host": endpoint_host(
                    service["connection_settings"].get("base_url")
                    or service["connection_settings"].get("endpoint")
                ),
            }
            for role, service in services.items()
        ],
        "embedding_configured": resolved.effective.embeddings is not None,
    }


async def dependency_report(org, target, purpose):
    try:
        return model_report(await prepare_models(org, target, purpose), purpose)
    except HTTPException as error:
        return {
            "ready": False,
            "purpose": purpose,
            "issues": [error.detail],
            "services": [],
        }

"""Resolve the actual scoped catalog binding before work and expose safe metadata."""

from fastapi import HTTPException

from api.db import db_client
from api.schemas.model_connections import ServiceSelection
from api.services.configuration.model_connections import (
    resolve_model_configuration,
    validate_service_selection,
)
from api.services.mps_policy import (
    check_definition_models,
    check_effective_models,
    endpoint_host,
)

BUILD_LLM_KEY = "agent_workbench_build_llm"
BUILD_LLM_PROVIDERS = ("openai", "openrouter", "atlascloud", "hopper")


async def get_build_model(org):
    row = await db_client.get_configuration(org, BUILD_LLM_KEY)
    return ServiceSelection.model_validate(row.value) if row and row.value else None


async def save_build_model(org, selection: ServiceSelection | None):
    if selection is not None:
        service = await validate_service_selection(org, "llm", selection)
        if service.provider not in BUILD_LLM_PROVIDERS:
            raise HTTPException(
                422, "The build assistant requires an OpenAI-compatible LLM connection"
            )
        if not service.model.strip():
            raise HTTPException(422, "Choose a model for the build assistant")
    # Only the catalog reference and model settings are saved. Workers read this
    # per build; neither the workflow nor the organization default is changed.
    await db_client.upsert_configuration(
        org, BUILD_LLM_KEY, selection.model_dump(mode="json") if selection else None
    )
    return selection


async def prepare_models(org, target, purpose):
    configurations = target.get("workflow_configurations") or {}
    check_definition_models(target.get("workflow_json"), configurations)
    build_override = None
    if purpose == "build":
        selection = await get_build_model(org)
        if selection:
            # Only a scoped catalog reference and model settings are persisted.
            # Runtime voice and evaluation continue to use the agent's own LLM.
            build_override = {"llm": selection.model_dump(mode="json")}
    resolved = await resolve_model_configuration(
        org,
        workflow_override=configurations.get("model_configuration_override"),
        api_override=build_override,
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
        resolved = await prepare_models(org, target, purpose)
        report = model_report(resolved, purpose)
        from api.services.workflow.runtime_profile import runtime_profile

        try:
            runtime = (
                await prepare_models(org, target, "evaluate")
                if purpose == "build"
                else resolved
            )
            report["runtime_profiles"] = [
                runtime_profile(
                    target.get("workflow_configurations"),
                    runtime.effective,
                    channel=channel,
                    definition=target.get("workflow_json"),
                )
                for channel in ("text", "browser", "phone")
            ]
        except HTTPException as error:
            report["runtime_issues"] = [error.detail]
        return report
    except HTTPException as error:
        return {
            "ready": False,
            "purpose": purpose,
            "issues": [error.detail],
            "services": [],
        }


async def replay_models(org, session_id, prior_id, purpose):
    """A fresh replay pins the original catalog settings without changing defaults."""
    from api.services.agent_workbench.errors import WorkbenchError
    from api.services.configuration.model_connections import (
        ResolvedModelConfiguration,
        hydrate_model_configuration_snapshot,
    )

    prior = await db_client.wb_get(org, session_id, "jobs", prior_id)
    key = (
        "evaluation_model_configuration_snapshot"
        if purpose == "evaluate"
        else "model_configuration_snapshot"
    )
    snapshot = prior["output"].get(key)
    if not snapshot:
        raise WorkbenchError("原测试没有完整模型快照，无法保持相同配置重跑。", 409)
    effective = await hydrate_model_configuration_snapshot(org, snapshot)
    check_effective_models(effective)
    return ResolvedModelConfiguration(
        effective=effective,
        snapshot=snapshot,
        provenance=snapshot.get("provenance", []),
    )

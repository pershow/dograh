"""Shared, secret-free account of the runtime's supported and resolved behavior."""

import hashlib
import json

from api.schemas.workflow_configurations import WorkflowConfigurationDefaults


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def runtime_profile(
    configurations,
    effective,
    *,
    channel,
    definition=None,
    observed=False,
    stt_config=None,
):
    config = WorkflowConfigurationDefaults.model_validate(configurations or {})
    realtime = (
        channel != "text"
        and bool(getattr(effective, "is_realtime", False))
        and getattr(effective, "realtime", None) is not None
    )
    models = {}
    model_settings = {}
    for role in ("llm", "stt", "tts", "realtime", "embeddings"):
        service = (
            stt_config
            if role == "stt" and stt_config is not None
            else getattr(effective, role, None)
        )
        if service is not None:
            models[role] = {
                key: getattr(service, key, None)
                for key in ("provider", "model", "voice", "voice_id", "language")
                if isinstance(getattr(service, key, None), (str, int, float, bool))
            }
            model_settings[role] = {
                key: getattr(service, key, None)
                for key in (
                    "provider",
                    "model",
                    "voice",
                    "voice_id",
                    "language",
                    "temperature",
                    "top_p",
                    "top_k",
                    "max_tokens",
                    "frequency_penalty",
                    "presence_penalty",
                    "seed",
                    "speed",
                    "sample_rate",
                    "encoding",
                    "api_version",
                    "dimensions",
                    "thinking_budget",
                )
                if isinstance(getattr(service, key, None), (str, int, float, bool))
            }
    capabilities = {
        "text": channel == "text",
        "audio": channel != "text",
        "local_context_compaction": not realtime,
        "agent_transfer": channel != "text" and not realtime,
        "local_turn_detection": channel != "text" and not realtime,
        "speech_cache": False,
    }
    settings = []
    external_turns = False
    if (
        not realtime
        and channel != "text"
        and getattr(effective, "stt", None) is not None
    ):
        from types import SimpleNamespace
        from api.services.pipecat.service_factory import stt_uses_external_turns

        external_turns = stt_uses_external_turns(
            SimpleNamespace(stt=stt_config or effective.stt)
        )

    def setting(key, requested, resolved, reason=""):
        settings.append(
            {
                "key": key,
                "requested": requested,
                "resolved": resolved,
                "status": "applied" if requested == resolved else "not_applied",
                "reason": reason,
            }
        )

    setting(
        "context_compaction_enabled",
        config.context_compaction_enabled,
        config.context_compaction_enabled and not realtime,
        "Realtime context is owned by the speech provider." if realtime else "",
    )
    setting(
        "tts_cache_enabled",
        config.tts_cache_enabled,
        False,
        "The active speech factory does not use the speech cache."
        if config.tts_cache_enabled
        else "",
    )
    start = (
        "external"
        if realtime
        or channel == "text"
        or (external_turns and config.turn_start_strategy == "default")
        else config.turn_start_strategy
    )
    setting(
        "turn_start_strategy",
        config.turn_start_strategy,
        start,
        "Text messages or the realtime provider delimit turns."
        if realtime or channel == "text"
        else "",
    )
    setting(
        "turn_stop_strategy",
        config.turn_stop_strategy,
        "external"
        if realtime or channel == "text" or external_turns
        else config.turn_stop_strategy,
        "Turn endings are supplied by the transport or speech provider."
        if realtime or channel == "text" or external_turns
        else "",
    )
    setting(
        "node_routing.mode",
        config.node_routing.mode,
        "off" if realtime else config.node_routing.mode,
        "Local node routing is unavailable in realtime mode." if realtime else "",
    )
    unsupported = [
        name
        for name in config.runtime_requirements
        if not capabilities.get(name, False)
    ]
    if (
        realtime
        and config.agent_behavior.enabled
        and any(
            policy.effect == "write"
            for policy in config.agent_behavior.tool_policies.values()
        )
    ):
        unsupported.append("guarded_business_actions")
    required_services = (
        ("llm",)
        if channel == "text"
        else ("realtime", "llm")
        if realtime
        else ("stt", "llm", "tts")
    )
    unsupported.extend(
        "service:" + role for role in required_services if role not in models
    )
    report = {
        "version": 1,
        "stage": "runtime" if observed else "preflight",
        "channel": channel,
        "pipeline": "realtime"
        if realtime
        else "text"
        if channel == "text"
        else "cascade",
        "models": models,
        "capabilities": capabilities,
        "settings": settings,
        "ready": not unsupported,
        "unsupported_requirements": unsupported,
        "definition_fingerprint": digest(definition or {}),
        "configuration_fingerprint": digest(config.model_dump(mode="json")),
        "model_fingerprint": digest(model_settings),
    }
    report["fingerprint"] = digest(report)
    return report


def require_runtime_capabilities(report):
    if not report["ready"]:
        from fastapi import HTTPException

        raise HTTPException(
            422,
            "Required runtime capabilities unavailable: "
            + ", ".join(report["unsupported_requirements"]),
        )

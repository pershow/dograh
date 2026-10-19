"""Managed-service boundaries must fail before HTTP, websocket or SDK construction."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api import constants
from api.services import mps_policy, organization_bootstrap
from api.services.agent_workbench.dependencies import dependency_report, prepare_models
from api.services.configuration import model_connections
from api.services.mps_service_key_client import MPSServiceKeyClient
from api.tests.test_model_connections import catalog as catalog_fixture
from api.tests.test_model_connections import connection, fallback_rule, pipeline

catalog = catalog_fixture


def test_non_oss_cannot_disable_hosted_authorization():
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import api.constants"],
        env={**os.environ, "MPS_ENABLED": "false", "DEPLOYMENT_MODE": "saas"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "supported only with DEPLOYMENT_MODE=oss" in result.stderr


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setattr(constants, "MPS_ENABLED", False)
    monkeypatch.setattr(constants, "MPS_API_URL", "https://managed.example.test/proxy")


@pytest.mark.parametrize(
    "provider,url",
    [
        ("dograh", None),
        ("openai", "https://services.dograh.com/api/v1/llm"),
        ("openai", "wss://SERVICES.DOGRAH.COM./stt"),
        ("openai", "managed.example.test:8443/proxy"),
    ],
)
def test_default_and_custom_managed_endpoints_are_blocked(disabled, provider, url):
    with pytest.raises(mps_policy.MPSDisabledError) as error:
        mps_policy.check_model_service("llm", provider=provider, base_url=url)
    assert error.value.detail["code"] == "mps_disabled"
    assert error.value.detail["role"] == "llm"


def test_byok_and_enabled_compatibility(disabled, monkeypatch):
    mps_policy.check_model_service(
        "llm",
        provider="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setattr(constants, "MPS_ENABLED", True)
    mps_policy.check_model_service("llm", provider="dograh")


async def test_control_plane_never_opens_http_clients_when_disabled(
    disabled, monkeypatch
):
    from api.services import mps_service_key_client as module

    async_client = Mock(side_effect=AssertionError("network must not start"))
    sync_client = Mock(side_effect=AssertionError("network must not start"))
    monkeypatch.setattr(module.httpx, "AsyncClient", async_client)
    monkeypatch.setattr(module.httpx, "Client", sync_client)
    client = MPSServiceKeyClient()
    for command in (
        client.get_service_keys,
        lambda: client.create_correlation_id(service_key="hidden"),
        lambda: client.create_service_key(name="test"),
    ):
        with pytest.raises(mps_policy.MPSDisabledError):
            await command()
    with pytest.raises(mps_policy.MPSDisabledError):
        client.validate_service_key("hidden")
    async_client.assert_not_called()
    sync_client.assert_not_called()


@pytest.mark.parametrize(
    "mps,bootstrap", [(False, True), (True, False), (False, False)]
)
async def test_new_organization_skips_all_managed_bootstrap_work(
    monkeypatch, mps, bootstrap
):
    monkeypatch.setattr(constants, "MPS_ENABLED", mps)
    monkeypatch.setattr(constants, "ENABLE_MANAGED_SERVICE_BOOTSTRAP", bootstrap)
    get = AsyncMock(
        side_effect=AssertionError("must not read or mutate bootstrap state")
    )
    monkeypatch.setattr(
        organization_bootstrap.db_client, "get_configuration_values", get
    )
    assert await organization_bootstrap.ensure_organization_bootstrapped(
        99, created_by="new-local-user"
    )
    get.assert_not_awaited()


async def test_factories_reject_managed_sdk_and_websocket_paths(disabled, monkeypatch):
    from api.services.gen_ai.embedding.factory import build_embedding_service
    from api.services.pipecat import service_factory as factory

    service = SimpleNamespace(provider="dograh", model="default")
    for create in (
        factory.create_stt_service,
        factory.create_tts_service,
        factory.create_realtime_llm_service,
    ):
        with pytest.raises(mps_policy.MPSDisabledError):
            create(SimpleNamespace(stt=service, tts=service, realtime=service), None)
    llm = Mock(side_effect=AssertionError("SDK must not be constructed"))
    monkeypatch.setattr(factory, "OpenAILLMService", llm)
    with pytest.raises(mps_policy.MPSDisabledError):
        factory.create_llm_service_from_provider(
            "openai", "qwen", "secret", base_url="https://managed.example.test/v1"
        )
    with pytest.raises(mps_policy.MPSDisabledError):
        await build_embedding_service(
            db_client=None,
            provider="openai",
            api_key="secret",
            model="embed",
            base_url="https://services.dograh.com/api/v1/llm",
        )
    llm.assert_not_called()


async def test_default_api_override_fallback_and_historical_hydration_are_checked(
    disabled, catalog
):
    add = catalog
    own = connection("openai")
    add(own, default=True, configuration=pipeline(own))
    managed = connection()
    add(managed)
    for kwargs in [
        {"workflow_override": {"llm": {"provider_connection_uuid": managed.uuid}}},
        {"api_override": {"llm": {"provider_connection_uuid": managed.uuid}}},
    ]:
        with pytest.raises(mps_policy.MPSDisabledError):
            await model_connections.resolve_model_configuration(1, **kwargs)
    resolved = await model_connections.resolve_model_configuration(1)
    from api.schemas.model_connections import PreCallModelOverride

    with pytest.raises(mps_policy.MPSDisabledError):
        await model_connections.resolve_pre_call_model_configuration(
            1,
            resolved.snapshot,
            resolved.effective,
            PreCallModelOverride.model_validate(
                {"llm": {"provider_connection_uuid": managed.uuid}}
            ),
        )
    backup = connection("openai")
    backup.connection_settings = {"base_url": "https://managed.example.test/v1"}
    add(backup)
    with pytest.raises(mps_policy.MPSDisabledError):
        await model_connections.resolve_model_configuration(
            1, api_override={"llm_fallback": {"rules": [fallback_rule(backup)]}}
        )
    # Historical execution must use pinned settings and check them too, rather
    # than substituting today's non-managed connection endpoint.
    resolved.snapshot["services"]["llm"]["connection_settings"]["base_url"] = (
        "https://services.dograh.com/api/v1/llm"
    )
    with pytest.raises(mps_policy.MPSDisabledError):
        await model_connections.hydrate_model_configuration_snapshot(
            1, resolved.snapshot
        )
    own.connection_settings["base_url"] = "https://managed.example.test/v1"
    with pytest.raises(mps_policy.MPSDisabledError):
        await model_connections.resolve_model_configuration(1)


async def test_preflight_has_safe_provenance_and_rejects_other_org_and_missing_roles(
    disabled, catalog
):
    add = catalog
    own = connection("openai", keys="credential-not-in-report")
    config = pipeline(own)
    config["llm"]["settings"]["model"] = "qwen"
    named = add(own, default=True, configuration=config)
    report = await dependency_report(1, {}, "build")
    assert report["ready"] and report["services"][0]["model"] == "qwen", report
    assert report["provenance"] and "credential-not-in-report" not in json.dumps(report)
    named.configuration = {"version": 3, "mode": "pipeline"}
    report = await dependency_report(1, {}, "voice")
    assert not report["ready"] and report["issues"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await prepare_models(
            999,
            {
                "workflow_configurations": {
                    "model_configuration_override": {
                        "llm": {"provider_connection_uuid": own.uuid}
                    }
                }
            },
            "build",
        )


@pytest.mark.parametrize(
    "graph,config",
    [
        (
            {
                "nodes": [
                    {
                        "id": "qa",
                        "data": {"qa_use_workflow_llm": False, "qa_provider": "dograh"},
                    }
                ]
            },
            {},
        ),
        (
            {},
            {
                "voicemail_detection": {
                    "enabled": True,
                    "use_workflow_llm": False,
                    "provider": "openai",
                    "endpoint": "https://services.dograh.com",
                }
            },
        ),
    ],
)
def test_explicit_qa_and_voicemail_are_preflighted(disabled, graph, config):
    with pytest.raises(mps_policy.MPSDisabledError):
        mps_policy.check_definition_models(graph, config)

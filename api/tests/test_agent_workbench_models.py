"""The build model is organization-scoped and cannot alter the voice runtime."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.services.agent_workbench import dependencies as module


@pytest.mark.parametrize("purpose", ["build", "voice", "evaluate", "tune"])
async def test_build_selection_does_not_change_runtime(monkeypatch, purpose):
    selection = {
        "provider_connection_uuid": "d2b61aa2-b458-409e-8239-03b854529f69",
        "settings": {"model": "builder"},
    }
    lookup = AsyncMock(return_value=SimpleNamespace(value=selection))
    monkeypatch.setattr(module.db_client, "get_configuration", lookup)
    resolved = SimpleNamespace(
        effective=SimpleNamespace(llm=object(), stt=object(), tts=object())
    )
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(module, "resolve_model_configuration", resolver)
    monkeypatch.setattr(module, "check_effective_models", lambda *_: None)
    override = {"llm": {"settings": {"model": "voice"}}}
    target = {"workflow_configurations": {"model_configuration_override": override}}
    assert await module.prepare_models(42, target, purpose) is resolved
    resolver.assert_awaited_once_with(
        42,
        workflow_override=override,
        api_override={"llm": selection} if purpose == "build" else None,
    )
    if purpose == "build":
        lookup.assert_awaited_once_with(42, module.BUILD_LLM_KEY)
    else:
        lookup.assert_not_awaited()
    assert target["workflow_configurations"]["model_configuration_override"] == override


async def test_missing_build_selection_preserves_workflow_model(monkeypatch):
    monkeypatch.setattr(
        module.db_client, "get_configuration", AsyncMock(return_value=None)
    )
    resolver = AsyncMock(
        return_value=SimpleNamespace(effective=SimpleNamespace(llm=object()))
    )
    monkeypatch.setattr(module, "resolve_model_configuration", resolver)
    monkeypatch.setattr(module, "check_effective_models", lambda *_: None)
    await module.prepare_models(17, {}, "build")
    assert resolver.call_args.kwargs["api_override"] is None


async def test_cross_org_or_archived_connection_is_not_silently_replaced(monkeypatch):
    monkeypatch.setattr(
        module.db_client,
        "get_configuration",
        AsyncMock(
            return_value=SimpleNamespace(
                value={
                    "provider_connection_uuid": "d2b61aa2-b458-409e-8239-03b854529f69",
                    "settings": {"model": "builder"},
                }
            )
        ),
    )
    resolver = AsyncMock(
        side_effect=HTTPException(404, "Provider connection not found")
    )
    monkeypatch.setattr(module, "resolve_model_configuration", resolver)
    with pytest.raises(HTTPException) as error:
        await module.prepare_models(17, {}, "build")
    assert error.value.status_code == 404
    assert resolver.call_args.args == (17,)

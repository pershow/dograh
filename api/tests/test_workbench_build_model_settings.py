"""Build model settings stay scoped to the authenticated organization."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api.routes import agent_workbench as routes
from api.services.agent_workbench import dependencies

CONNECTION = "d2b61aa2-b458-409e-8239-03b854529f69"
SELECTION = {"provider_connection_uuid": CONNECTION, "settings": {"model": "builder"}}
URL = "/agent-workbench/build-model"


@pytest.fixture
async def settings_api(monkeypatch):
    stored = {}

    async def read(org, key):
        assert org == 42 and key == dependencies.BUILD_LLM_KEY
        return SimpleNamespace(value=stored.get(key))

    async def write(org, key, value):
        assert org == 42 and key == dependencies.BUILD_LLM_KEY
        stored[key] = value

    connection = SimpleNamespace(
        uuid=CONNECTION,
        provider="openai",
        credentials={"api_key": "private-provider-secret"},
        connection_settings={},
    )
    lookup = AsyncMock(return_value=connection)
    save = AsyncMock(side_effect=write)
    monkeypatch.setattr(
        dependencies.db_client, "get_configuration", AsyncMock(side_effect=read)
    )
    monkeypatch.setattr(dependencies.db_client, "upsert_configuration", save)
    monkeypatch.setattr(dependencies.db_client, "get_provider_connection", lookup)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_user_with_selected_organization] = lambda: (
        SimpleNamespace(selected_organization_id=42)
    )
    app.dependency_overrides[routes.require_enabled] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, lookup, save, connection, app


async def test_save_read_and_reset_without_touching_agent_settings(settings_api):
    client, lookup, save, _, _ = settings_api
    assert (await client.get(URL)).json()["llm"] is None
    response = await client.put(URL, json={"llm": SELECTION})
    assert response.status_code == 200
    assert response.json()["llm"] == SELECTION
    assert response.json()["supported_providers"] == list(
        dependencies.BUILD_LLM_PROVIDERS
    )
    assert "private-provider-secret" not in response.text
    lookup.assert_awaited_once_with(42, CONNECTION, active_only=True)
    save.assert_awaited_once_with(42, dependencies.BUILD_LLM_KEY, SELECTION)
    assert (await client.get(URL)).json()["llm"] == SELECTION
    response = await client.put(URL, json={"llm": None})
    assert response.status_code == 200
    assert (await client.get(URL)).json()["llm"] is None
    assert save.await_args.args == (42, dependencies.BUILD_LLM_KEY, None)


async def test_foreign_or_archived_connection_is_rejected(settings_api):
    client, lookup, save, _, _ = settings_api
    lookup.return_value = None
    response = await client.put(URL, json={"llm": SELECTION})
    assert response.status_code == 404
    lookup.assert_awaited_once_with(42, CONNECTION, active_only=True)
    save.assert_not_awaited()


async def test_unsupported_provider_is_rejected(settings_api):
    client, _, save, connection, _ = settings_api
    connection.provider = "google"
    response = await client.put(URL, json={"llm": SELECTION})
    assert response.status_code == 422
    assert "OpenAI-compatible" in response.text
    save.assert_not_awaited()


@pytest.mark.parametrize(
    "settings",
    [
        {"model": "   "},
        {"model": "builder", "unknown": True},
        {"model": "builder", "temperature": "invalid"},
        {"model": "builder", "api_key": "not-allowed"},
        {"model": "builder", "base_url": "https://example.com"},
    ],
)
async def test_invalid_settings_are_not_persisted(settings_api, settings):
    client, _, save, _, _ = settings_api
    response = await client.put(URL, json={"llm": {**SELECTION, "settings": settings}})
    assert response.status_code == 422
    save.assert_not_awaited()


async def test_read_and_write_require_enabled_workbench(settings_api, monkeypatch):
    client, _, save, _, app = settings_api
    app.dependency_overrides.pop(routes.require_enabled)
    monkeypatch.setattr(
        routes.WorkbenchSettings, "load", lambda: SimpleNamespace(enabled=False)
    )
    assert (await client.get(URL)).status_code == 404
    assert (await client.put(URL, json={"llm": SELECTION})).status_code == 404
    save.assert_not_awaited()


async def test_missing_body_does_not_accidentally_reset_selection(settings_api):
    client, _, save, _, _ = settings_api
    assert (await client.put(URL, json={})).status_code == 422
    save.assert_not_awaited()

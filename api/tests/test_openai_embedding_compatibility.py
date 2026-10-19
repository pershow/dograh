from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.configuration.ai_model_configuration import (
    get_resolved_ai_model_configuration,
)
from api.services.configuration.model_connections import resolve_model_configuration
from api.services.gen_ai.embedding.openai_service import OpenAIEmbeddingService
from api.tests.test_model_connections import catalog as catalog_fixture
from api.tests.test_model_connections import connection, pipeline, selection

catalog = catalog_fixture


async def test_dashscope_connection_url_and_dimensions_survive_catalog(catalog):
    managed = connection()
    catalog(managed)
    qwen = connection(provider="openai")
    qwen.connection_settings = {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    }
    catalog(qwen)
    spec = pipeline(managed)
    spec["embeddings"] = selection(qwen, model="text-embedding-v4", dimensions=1536)
    catalog(managed, default=True, configuration=spec)
    result = await resolve_model_configuration(1)
    assert result.effective.embeddings.base_url == qwen.connection_settings["base_url"]
    assert result.effective.embeddings.dimensions == 1536
    assert (
        result.snapshot["services"]["embeddings"]["connection_settings"]["base_url"]
        == qwen.connection_settings["base_url"]
    )
    default = await get_resolved_ai_model_configuration(organization_id=1)
    assert (
        default.snapshot == result.snapshot and default.provenance == result.provenance
    )


async def test_requested_dimensions_order_and_no_managed_metadata():
    svc = OpenAIEmbeddingService(
        None,
        api_key="test",
        model_id="text-embedding-v4",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        dimensions=1536,
    )
    create = AsyncMock(
        return_value=SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[0.2] * 1536),
                SimpleNamespace(index=0, embedding=[0.1] * 1536),
            ]
        )
    )
    svc.client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    result = await svc.embed_texts(["first", "second"])
    assert result[0][0] == 0.1 and result[1][0] == 0.2
    assert create.await_args.kwargs == {
        "input": ["first", "second"],
        "model": "text-embedding-v4",
        "dimensions": 1536,
    }


@pytest.mark.parametrize(
    "items",
    [
        [SimpleNamespace(index=0, embedding=[0.1] * 1024)],
        [SimpleNamespace(index=0, embedding=[float("nan")] * 1536)],
        [],
    ],
)
async def test_invalid_embedding_response_rejected(items):
    svc = OpenAIEmbeddingService(None, api_key="test")
    svc.client = SimpleNamespace(
        embeddings=SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(data=items))
        )
    )
    with pytest.raises(ValueError):
        await svc.embed_texts(["x"])

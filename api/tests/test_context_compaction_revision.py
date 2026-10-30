from types import SimpleNamespace
from unittest.mock import AsyncMock

from pipecat.processors.aggregators.llm_context import LLMContext
from api.services.workflow import pipecat_engine_context_summarizer as module


def setup():
    context = LLMContext(
        messages=[
            {"role": "system", "content": "business rules"},
            *[{"role": "user", "content": "x" * 1200} for _ in range(10)],
        ]
    )
    llm = SimpleNamespace(_generate_summary=AsyncMock(return_value=("summary", 5)))
    agent = SimpleNamespace(
        current_node=SimpleNamespace(id="main", name="main"), inference_llm=llm
    )
    engine = SimpleNamespace(
        context=context, active_agent=agent, _get_otel_context=lambda: None
    )
    manager = module.ContextSummarizationManager(engine)
    manager.trigger_chars = 4000
    return context, llm, manager


async def test_same_node_compacts_after_budget_is_crossed(monkeypatch):
    monkeypatch.setattr(module, "ensure_tracing", lambda: False)
    context, llm, manager = setup()
    await manager.compact_if_needed(wait=True)
    llm._generate_summary.assert_awaited_once()
    assert len(context.messages) < 11
    assert context.messages[0]["content"] == "business rules"


async def test_inflight_correction_invalidates_summary(monkeypatch):
    monkeypatch.setattr(module, "ensure_tracing", lambda: False)
    context, llm, manager = setup()

    async def summarize(_request):
        context.messages[2]["content"] = "Correction: Thursday"
        return "Old Friday summary", 5

    llm._generate_summary.side_effect = summarize
    await manager.compact_if_needed(wait=True)
    assert len(context.messages) == 11
    assert context.messages[2]["content"] == "Correction: Thursday"


async def test_appended_turn_is_preserved(monkeypatch):
    monkeypatch.setattr(module, "ensure_tracing", lambda: False)
    context, llm, manager = setup()

    async def summarize(_request):
        context.add_message({"role": "user", "content": "Actually change the time"})
        return "Earlier conversation", 5

    llm._generate_summary.side_effect = summarize
    await manager.compact_if_needed(wait=True)
    assert context.messages[-1]["content"] == "Actually change the time"

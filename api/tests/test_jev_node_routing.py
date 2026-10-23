import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.classifiers.base_classifier import ChoiceResult
from pipecat.frames.frames import LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.mock_llm_service import ContextCapturingMockLLM
from pipecat.tests.utils import SleepFrame
from pydantic import ValidationError

from api.schemas.workflow_configurations import JevNodeRoutingConfiguration
from api.services.pipecat.node_routing import NodeRoutingProcessor
from api.services.workflow.pipecat_engine import PipecatEngine
from pipecat.tests import run_test

DOWN = FrameDirection.DOWNSTREAM


def answer(choice="edge_0", confidence=0.95):
    return {
        "route": ChoiceResult(
            choice=choice, confidence=confidence, probabilities={choice: confidence}
        )
    }


@pytest.fixture
async def routing(three_node_workflow_no_variable_extraction):
    context = LLMContext(
        [
            {"role": "assistant", "content": "您是商家吗？"},
            {"role": "user", "content": "是的"},
        ]
    )
    llm = Mock(_update_settings=AsyncMock())
    engine = PipecatEngine(
        llm=llm,
        context=context,
        workflow=three_node_workflow_no_variable_extraction,
        task=SimpleNamespace(queue_frame=AsyncMock()),
        call_context_vars={},
        node_routing=JevNodeRoutingConfiguration(mode="on", timeout_ms=50),
    )
    await engine.set_node("start")
    engine._perform_variable_extraction_if_needed = AsyncMock()
    router = NodeRoutingProcessor(engine)
    router.push_frame = AsyncMock()
    tasks = set()

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def cancel_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    router.create_task = create_task
    router.cancel_task = cancel_task
    classifier = SimpleNamespace(choice=AsyncMock(return_value=answer()))
    router._classifier_for = AsyncMock(return_value=classifier)
    yield engine, context, router, classifier
    for task in list(tasks):
        await cancel_task(task)


async def send(router, context):
    frame = LLMContextFrame(context)
    await router.process_frame(frame, DOWN)
    if router._pending is not None:
        await router._pending
    return frame


@pytest.mark.asyncio
async def test_confident_transition_preserves_engine_effects_and_context(routing):
    engine, context, router, classifier = routing
    original_node = engine.active_agent.current_node
    await send(router, context)
    assert engine.active_agent.current_node.id == "agent"
    engine._perform_variable_extraction_if_needed.assert_awaited_once_with(
        original_node, run_in_background=True
    )
    assert len(context.messages) == 4
    assert context.messages[-2]["tool_calls"][0]["id"].startswith("jev-route-")
    assert json.loads(context.messages[-1]["content"])["current_node_id"] == "agent"
    request = router.push_frame.await_args.args[0].context
    assert request.messages is context.messages
    assert (
        not isinstance(request.tools, ToolsSchema) or not request.tools.standard_tools
    )
    assert (
        context.tools.standard_tools
    )  # Next user turn can still use transition tools.
    assert engine._gathered_context["node_routing"][-1]["outcome"] == "transition"
    state = classifier.choice.await_args.args[0]
    assert "Start Call System Prompt" in state["instructions"]


@pytest.mark.asyncio
async def test_confident_stay_retains_business_tools_and_canonical_history(routing):
    engine, context, router, classifier = routing
    classifier.choice.return_value = answer("stay")
    original = copy.deepcopy(context.messages)
    tools = [*context.tools.standard_tools, FunctionSchema("lookup", "Look up", {}, [])]
    context.set_tools(ToolsSchema(tools))
    await send(router, context)
    forwarded = router.push_frame.await_args.args[0].context
    assert [t.name for t in forwarded.tools.standard_tools] == ["lookup"]
    assert len(context.tools.standard_tools) == len(tools)
    assert context.messages == original
    assert engine.active_agent.current_node.id == "start"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choice,confidence,reason",
    [
        ("edge_0", 0.4, "low_confidence"),
        ("uncertain", 0.99, "uncertain"),
        ("missing", 0.99, "invalid_result"),
        ("edge_0", float("nan"), "invalid_result"),
    ],
)
async def test_uncertain_results_preserve_the_original_llm_request(
    routing, choice, confidence, reason
):
    engine, context, router, classifier = routing
    classifier.choice.return_value = answer(choice, confidence)
    history = copy.deepcopy(context.messages)
    frame = await send(router, context)
    assert router.push_frame.await_args.args[0] is frame
    assert context.messages == history
    assert engine.active_agent.current_node.id == "start"
    assert engine._gathered_context["node_routing"][-1]["outcome"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "error", "missing_api_key"])
async def test_failure_falls_back_without_retry_or_context_mutation(
    routing, failure, monkeypatch
):
    engine, context, router, classifier = routing
    if failure == "timeout":

        async def slow(*args):
            await asyncio.sleep(10)

        classifier.choice.side_effect = slow
    elif failure == "error":
        classifier.choice.side_effect = RuntimeError("sensitive provider response")
    else:
        monkeypatch.setattr("api.constants.TYPESAFE_API_KEY", "")
        router._classifier_for = NodeRoutingProcessor._classifier_for.__get__(router)
    frame = await asyncio.wait_for(send(router, context), 0.5)
    assert router.push_frame.await_args.args[0] is frame
    assert engine.active_agent.current_node.id == "start"
    record = engine._gathered_context["node_routing"][-1]
    assert record["outcome"] == failure
    assert "sensitive" not in json.dumps(record)
    assert classifier.choice.await_count <= 1
    if failure == "timeout":
        assert record["failure_phase"] == "inference"


@pytest.mark.asyncio
async def test_connection_timeout_does_not_cancel_warmup_or_reconnect_next_turn(
    routing, monkeypatch
):
    from api.services.pipecat import jev_client_pool as pool

    engine, context, router, _ = routing
    entered, connected = asyncio.Event(), asyncio.Event()
    client = SimpleNamespace(close=AsyncMock())

    async def connect():
        if not connected.is_set():
            entered.set()
            await connected.wait()

    client.connect = AsyncMock(side_effect=connect)
    factory = Mock(return_value=client)
    monkeypatch.setattr(pool, "JevClient", factory)
    monkeypatch.setattr("api.constants.TYPESAFE_API_KEY", "test-credential")
    monkeypatch.setattr(
        "api.services.pipecat.node_routing.JevClassifier",
        Mock(
            side_effect=lambda **kw: SimpleNamespace(
                client=kw["client"],
                setup=AsyncMock(),
                cleanup=AsyncMock(),
                choice=AsyncMock(return_value=answer()),
            )
        ),
    )
    router._task_manager = Mock()
    router._classifier_for = NodeRoutingProcessor._classifier_for.__get__(router)
    next_router = None
    try:
        connection = router._connection_for(engine.active_agent.node_routing)
        await entered.wait()
        await send(router, context)
        record = engine._gathered_context["node_routing"][-1]
        assert record["outcome"] == "timeout"
        assert record["failure_phase"] == "connection"
        assert not connection.warmup.done()
        await router.cleanup()
        client.close.assert_not_awaited()

        connected.set()
        await connection.warmup
        next_router = NodeRoutingProcessor(engine)
        next_router._task_manager = Mock()
        classifier = await next_router._classifier_for(engine.active_agent.node_routing)
        assert classifier.client is client
        assert factory.call_count == 1
        result = await next_router._safe_decide(
            engine.active_agent,
            context,
            next_router._record(engine.active_agent, "new"),
        )
        assert result.target == "agent"
    finally:
        await router.cleanup()
        if next_router is not None:
            await next_router.cleanup()
        await pool.close_jev_connections()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_trace_contains_decision_input_output_and_timeout_stage(
    routing, monkeypatch
):
    _engine, context, router, _classifier = routing
    span = Mock()
    tracer = Mock()
    tracer.start_as_current_span.return_value.__enter__ = Mock(return_value=span)
    tracer.start_as_current_span.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(
        "api.services.pipecat.node_routing.trace.get_tracer", Mock(return_value=tracer)
    )
    await send(router, context)
    attrs = dict(call.args for call in span.set_attribute.call_args_list)
    assert json.loads(attrs["input"])["state"]["dialogue"][-1]["content"] == "是的"
    assert json.loads(attrs["output"])["choice"] == "edge_0"
    assert json.loads(attrs["output"])["confidence"] == 0.95


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["off", "realtime", "opening", "tool", "duplicate", "large"]
)
async def test_ineligible_turns_do_not_call_jev(routing, case):
    engine, context, router, classifier = routing
    if case == "off":
        engine.active_agent.node_routing.mode = "off"
    elif case == "realtime":
        engine._is_realtime = True
    elif case == "opening":
        context.set_messages([])
    elif case == "tool":
        context.add_message({"role": "tool", "content": "done"})
    elif case == "large":
        context.messages[-1]["content"] = "x" * 40_000
    else:
        from api.services.pipecat.node_routing import _signature

        router._last_signature = _signature(context)
    await send(router, context)
    classifier.choice.assert_not_awaited()
    router.push_frame.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_pending_frame_generates_only_once(routing):
    _engine, context, router, classifier = routing
    entered = asyncio.Event()
    released = asyncio.Event()

    async def wait(*args):
        entered.set()
        await released.wait()
        return answer()

    classifier.choice.side_effect = wait
    await router.process_frame(LLMContextFrame(context), DOWN)
    await asyncio.wait_for(entered.wait(), 1)
    await router.process_frame(LLMContextFrame(context), DOWN)
    router.push_frame.assert_not_awaited()
    released.set()
    await asyncio.wait_for(router._pending, 1)
    classifier.choice.assert_awaited_once()
    router.push_frame.assert_awaited_once()


@pytest.mark.asyncio
async def test_shadow_is_nonblocking_and_compares_without_changing_the_response(
    routing,
):
    engine, context, router, classifier = routing
    engine.active_agent.node_routing.mode = "shadow"
    released = asyncio.Event()

    async def wait(*args):
        await released.wait()
        return answer()

    classifier.choice.side_effect = wait
    frame = await send(router, context)
    router.push_frame.assert_awaited_once_with(frame, DOWN)
    assert engine.active_agent.current_node.id == "start"
    record = engine._gathered_context["node_routing"][-1]
    # Simulate the original LLM making its own decision before Jev returns.
    engine.active_agent.current_node = engine.active_agent.workflow.nodes["agent"]
    router._finish_shadow()
    released.set()
    await asyncio.gather(*list(router._shadows))
    assert record["predicted_node_id"] == "agent"
    assert record["llm_target_node_id"] == "agent"
    assert record["agrees_with_llm"] is True
    assert record["outcome"] == "shadow"
    assert len(context.messages) == 2


@pytest.mark.asyncio
async def test_stale_agent_decision_cannot_move_new_owner(routing):
    engine, context, router, classifier = routing

    async def retire(*args):
        engine._agent_on_hold = True
        return answer()

    classifier.choice.side_effect = retire
    await send(router, context)
    router.push_frame.assert_not_awaited()
    assert engine.active_agent.current_node.id == "start"
    assert engine._gathered_context["node_routing"][-1]["outcome"] == "stale"


@pytest.mark.asyncio
async def test_cancellation_does_not_fall_back_to_an_unwanted_response(routing):
    engine, context, router, classifier = routing
    entered = asyncio.Event()

    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()

    classifier.choice.side_effect = wait
    await router.process_frame(LLMContextFrame(context), DOWN)
    await asyncio.wait_for(entered.wait(), 1)
    await router._cancel_pending()
    assert engine._gathered_context["node_routing"][-1]["outcome"] == "cancelled"
    router.push_frame.assert_not_awaited()
    assert engine.active_agent.current_node.id == "start"


@pytest.mark.asyncio
async def test_end_node_reuses_playback_completion(routing):
    engine, context, router, _classifier = routing
    await engine.set_node("agent")
    playback = SimpleNamespace(wait=AsyncMock())
    engine.speech_playback.expect_response = Mock(return_value=playback)
    engine.end_call_with_reason = AsyncMock()
    await send(router, context)
    await asyncio.sleep(0)
    assert engine.active_agent.current_node.id == "end"
    playback.wait.assert_awaited_once()
    engine.end_call_with_reason.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_pipeline_generates_only_the_destination_reply(
    three_node_workflow_no_variable_extraction,
):
    llm = ContextCapturingMockLLM(
        mock_chunks=ContextCapturingMockLLM.create_text_chunks("下一步"), chunk_delay=0
    )
    context = LLMContext([{"role": "user", "content": "是的"}])
    engine = PipecatEngine(
        llm=llm,
        context=context,
        workflow=three_node_workflow_no_variable_extraction,
        call_context_vars={},
        node_routing=JevNodeRoutingConfiguration(mode="on"),
    )
    await engine.set_node("start")
    router = NodeRoutingProcessor(engine)
    router._classifier_for = AsyncMock(
        return_value=SimpleNamespace(choice=AsyncMock(return_value=answer()))
    )
    await run_test(
        Pipeline([router, llm]),
        frames_to_send=[LLMContextFrame(context), SleepFrame(sleep=0.15)],
    )
    assert len(llm.captured_contexts) == 1
    assert "Agent Node System Prompt" in llm.captured_contexts[0]["system_prompt"]
    assert engine.active_agent.current_node.id == "agent"


@pytest.mark.parametrize(
    "config",
    [
        {"timeout_ms": 0},
        {"confidence_threshold": 1.1},
        {"mode": "invalid"},
        {"api_key": "secret"},
    ],
)
def test_configuration_rejects_invalid_or_secret_values(config):
    with pytest.raises(ValidationError):
        JevNodeRoutingConfiguration.model_validate(config)

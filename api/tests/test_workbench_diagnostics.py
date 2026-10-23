import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from api.services.agent_workbench import diagnostics as d
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.tools import WorkbenchTools


@pytest.fixture
def packet():
    return {
        "run": {
            "id": 42,
            "definition_id": 10,
            "initial_context": {"name": "Alice"},
            "model_configuration_snapshot": {
                "version": 3,
                "services": {"llm": {"settings": {"model": "frozen"}}},
            },
        },
        "definition": {
            "workflow_json": {
                "nodes": [
                    {
                        "id": "g",
                        "type": "globalNode",
                        "position": {"x": 0, "y": 0},
                        "data": {"name": "Shared", "prompt": "Help {{name}}."},
                    },
                    {
                        "id": "s",
                        "type": "startCall",
                        "position": {"x": 0, "y": 0},
                        "data": {
                            "name": "Verify",
                            "prompt": "Confirm identity then call proceed.",
                            "greeting": "Hello",
                        },
                    },
                    {
                        "id": "n",
                        "type": "agentNode",
                        "position": {"x": 0, "y": 1},
                        "data": {
                            "name": "Next",
                            "prompt": "Ask for the reference number.",
                        },
                    },
                ],
                "edges": [
                    {
                        "id": "e",
                        "source": "s",
                        "target": "n",
                        "data": {"label": "proceed", "condition": "Identity confirmed"},
                    }
                ],
            }
        },
        "case": {
            "transcript": [
                {"role": "assistant", "text": "Hello", "turn": 0},
                {"role": "user", "text": "Yes, that is me", "turn": 1},
                {"role": "assistant", "text": "Invented next step", "turn": 1},
            ],
            "events": [
                {
                    "type": "node_transition",
                    "turn": 0,
                    "payload": {"node_id": "s", "node_name": "Verify"},
                }
            ],
        },
    }


@pytest.fixture
def owner(monkeypatch, packet):
    monkeypatch.setattr(
        d.db_client, "wb_diagnostic_run", AsyncMock(return_value=packet)
    )
    monkeypatch.setattr(d.db_client, "wb_emit", AsyncMock())
    monkeypatch.setattr(d.db_client, "wb_diagnostic_probes", AsyncMock(return_value=[]))
    monkeypatch.setattr(d.db_client, "wb_heartbeat", AsyncMock())
    return WorkbenchTools(
        1,
        "s",
        {"id": "j", "lease_token": "t", "input": {"mode": "analyze"}},
        {"target": {}, "target_fingerprint": "fp"},
    )


async def test_old_request_uses_recorded_node_not_later_response_or_current_draft(
    packet,
):
    result = await d.reconstruct_request(packet, 1)
    assert result["node_id"] == "s" and result["provenance"] == "reconstructed"
    assert result["replayable"]
    request = result["request"]
    assert "Help Alice" in request["messages"][0]["content"]
    assert request["messages"][-1] == {"role": "user", "content": "Yes, that is me"}
    assert "Invented next step" not in json.dumps(request)
    assert [t["function"]["name"] for t in request["tools"]] == ["proceed"]


async def test_missing_history_blocks_replay_and_capture_takes_precedence(packet):
    packet["case"]["events"].append(
        {"type": "node_transition", "turn": 0, "payload": {"node_id": "n"}}
    )
    assert not (await d.reconstruct_request(packet, 1))["replayable"]
    request = {
        "messages": [
            {"role": "system", "content": "Captured exact"},
            {"role": "tool", "tool_call_id": "c", "content": "ok"},
        ]
    }
    packet["case"]["events"].append(
        {
            "type": "llm_request",
            "turn": 1,
            "payload": {"node_id": "n", "request": request, "redacted": False},
        }
    )
    result = await d.reconstruct_request(packet, 1)
    assert (
        result["provenance"] == "captured"
        and result["request"] == request
        and result["replayable"]
    )
    packet["case"]["events"][-1]["payload"]["redacted"] = True
    assert not (await d.reconstruct_request(packet, 1))["replayable"]


async def test_absent_node_is_explicit_gap_not_invented_start(packet, owner):
    packet["case"]["events"] = []
    await owner.start_diagnosis("Why the response?", [42])
    evidence = await owner.inspect_run_diagnostic(run_id=42, section="request", turn=1)
    assert evidence["provenance"] == "unavailable"
    assert not evidence["replayable"]
    assert "No recorded entry node" in evidence["text"]


async def test_execution_summary_survives_tiny_page_and_large_review(
    monkeypatch, packet
):
    tools = WorkbenchTools(
        1,
        "s",
        {"id": "j", "lease_token": "t", "input": {"mode": "analyze"}},
        {"target": {}, "target_fingerprint": "f"},
    )
    case = {**packet["case"], "run_id": 42, "reference_review": {"text": "x" * 80000}}
    monkeypatch.setattr(
        d.db_client, "wb_get", AsyncMock(return_value={"id": "e", "results": [case]})
    )
    monkeypatch.setattr(d.db_client, "wb_emit", AsyncMock())
    result = await tools.read_evaluation("e", 0, limit=30)
    assert result["execution"]["transitions"][0]["node_id"] == "s"
    execution = await tools.read_evaluation("e", 0, section="execution")
    assert (
        "node_transition" in execution["text"]
        and "reference_review" not in execution["text"]
    )


async def test_paired_probe_preserves_history_frozen_model_and_agent_no_tool_execution(
    owner, packet, monkeypatch
):
    from api.services.configuration import model_connections
    from api.services.pipecat import service_factory

    original = copy.deepcopy(packet)
    config = SimpleNamespace(llm=SimpleNamespace(provider="openai", model="frozen"))
    hydrate = AsyncMock(return_value=config)
    monkeypatch.setattr(
        model_connections, "hydrate_model_configuration_snapshot", hydrate
    )
    calls = []

    def factory(cfg):
        assert cfg is config
        return SimpleNamespace(
            build_chat_completion_params=lambda request: copy.deepcopy(request),
            _client=SimpleNamespace(close=AsyncMock()),
        )

    async def completion(service, params):
        calls.append(params)
        return {
            "text": "",
            "tool_calls": [{"name": "dangerous_external_tool", "arguments": "{}"}],
        }

    monkeypatch.setattr(service_factory, "create_llm_service", factory)
    monkeypatch.setattr(d, "collect_completion", completion)
    await owner.start_diagnosis("Why skipped?", [42])
    result = await owner.run_diagnostic_probe(
        run_id=42,
        turn=1,
        hypothesis="Wrong context",
        prediction="Different reply",
        node_id="n",
        repeats=2,
    )
    assert len(calls) == 4 and result["agent_modified"] is False
    assert packet == original
    assert calls[0]["messages"][1:] == calls[1]["messages"][1:]
    assert calls[0]["messages"][0] != calls[1]["messages"][0]
    hydrate.assert_awaited_once_with(1, packet["run"]["model_configuration_snapshot"])
    assert len(result["results"]) == 4
    assert result["results"][0]["tool_calls"][0]["name"] == "dangerous_external_tool"
    with pytest.raises(WorkbenchError, match="one factor"):
        await owner.run_diagnostic_probe(
            run_id=42,
            turn=1,
            hypothesis="h",
            prediction="p",
            node_id="n",
            edits=[{"old": "a", "new": "b"}],
        )
    owner.diagnostics.requests = 24
    with pytest.raises(WorkbenchError, match="budget"):
        await owner.run_diagnostic_probe(
            run_id=42, turn=1, hypothesis="h", prediction="p", node_id="n"
        )


async def test_conclusion_requires_effective_context_and_real_evidence(owner):
    await owner.start_diagnosis("Why?", [42])
    report = {
        "status": "concluded",
        "findings": [
            {
                "claim": "cause",
                "status": "supported",
                "evidence_ids": ["fabricated"],
                "reason": "x",
            }
        ],
        "cause_chain": ["x"],
        "limitations": [],
        "next_steps": [],
    }
    with pytest.raises(WorkbenchError, match="cite inspected"):
        owner.diagnostics.validate_report(report)
    report["findings"][0]["evidence_ids"] = list(owner.diagnostics.reads)
    with pytest.raises(WorkbenchError, match="effective request"):
        owner.diagnostics.validate_report(report)
    await owner.inspect_run_diagnostic(run_id=42, section="request", turn=1)
    owner.diagnostics.validate_report(report)


async def test_diagnostic_evaluation_preserves_failure_stage_and_unsent_input(owner, packet):
    packet["source"] = {"session_id": "historical-session", "evaluation_id": "ev", "case_index": 0}
    packet["case"].update(
        status="error", error="Structured reviewer output invalid",
        runtime_responded=True,
        validity={"status": "inconclusive", "checks": [
            {"proposal": {"text": "Assumed success"}, "delivered": False},
        ]},
    )
    original = copy.deepcopy(packet)
    owner.specs()
    overview = await owner.call("read_agent_run", {"action": "inspect_run_diagnostic", "run_id": 42, "section": "overview"})
    assert "Structured reviewer output invalid" in overview["text"]
    pages, offset = [], 0
    while True:
        result = await owner.call("read_agent_run", {"action": "inspect_run_diagnostic", "run_id": 42, "section": "evaluation", "offset": offset, "limit": 180})
        pages.append(result["text"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    evidence = json.loads("".join(pages))
    assert evidence["source"]["session_id"] == "historical-session"
    assert evidence["result"]["runtime_responded"] is True
    assert evidence["result"]["validity"]["checks"][0]["delivered"] is False
    assert "transcript" not in evidence["result"]
    assert packet == original
    assert owner.diagnostics.reads[result["evidence_id"]]["complete"] is True


async def test_verifier_rejects_factually_supported_but_incomplete_diagnosis(
    owner, monkeypatch
):
    from api.services.agent_workbench import test_tasks

    owner.context["session"] = {"model_protocol": "chat"}
    owner.analysis_reviewer = object()
    owner.analysis_event = AsyncMock()
    await owner.start_diagnosis("Why?", [42])
    await owner.inspect_run_diagnostic(run_id=42, section="request", turn=1)
    report = {
        "status": "concluded",
        "findings": [
            {
                "claim": "cause",
                "status": "supported",
                "evidence_ids": list(owner.diagnostics.reads),
                "reason": "x",
            }
        ],
        "cause_chain": ["x"],
        "limitations": [],
        "next_steps": [],
    }
    monkeypatch.setattr(
        test_tasks,
        "decision",
        AsyncMock(
            return_value={
                "supported": True,
                "complete": False,
                "feedback": "Inspect available causal alternatives",
            }
        ),
    )
    with pytest.raises(WorkbenchError, match="证据检查未通过"):
        await owner.finish_analysis("Observed a mismatch", diagnosis=report)
    assert owner.analysis_answer is None


async def test_request_capture_excludes_credentials_without_mutating_http_body():
    emit = Mock()
    service = SimpleNamespace(
        _client=SimpleNamespace(
            api_key="secret-value", _client=SimpleNamespace(event_hooks={})
        )
    )
    engine = SimpleNamespace(
        active_agent=SimpleNamespace(
            current_node=SimpleNamespace(id="s", name="Verify")
        )
    )
    assert d.install_request_capture(service, engine, emit)
    req = httpx.Request(
        "POST",
        "https://example.test/v1/chat/completions",
        headers={"Authorization": "Bearer secret-value"},
        json={
            "messages": [{"role": "user", "content": "secret-value"}],
            "api_key": "secret-value",
        },
    )
    original = req.content
    await service._client._client.event_hooks["request"][0](req)
    saved = emit.call_args.args[1]
    assert saved["redacted"] and "secret-value" not in json.dumps(saved)
    assert (
        req.content == original
        and req.headers["Authorization"] == "Bearer secret-value"
    )


async def test_old_thread_tool_name_dispatches_new_actions_and_requires_all_request_pages(
    owner,
):
    spec = next(s for s in owner.specs() if s["name"] == "read_agent_run")
    assert "run_diagnostic_probe" in spec["inputSchema"]["properties"]["action"]["enum"]
    assert not any(s["name"] == "start_diagnosis" for s in owner.specs(for_thread=True))
    await owner.call(
        "read_agent_run",
        {"action": "start_diagnosis", "question": "Why?", "run_ids": [42]},
    )
    evidence = await owner.call(
        "read_agent_run",
        {
            "action": "inspect_run_diagnostic",
            "run_id": 42,
            "section": "request",
            "turn": 1,
            "limit": 100,
        },
    )
    report = {
        "status": "concluded",
        "findings": [
            {
                "claim": "cause",
                "status": "supported",
                "evidence_ids": [evidence["evidence_id"]],
            }
        ],
    }
    with pytest.raises(WorkbenchError, match="complete effective request"):
        owner.diagnostics.validate_report(report)
    # Merely reading the tail must not fill the unread middle.
    await owner.inspect_run_diagnostic(
        run_id=42, section="request", turn=1, offset=evidence["characters"] - 10
    )
    with pytest.raises(WorkbenchError, match="complete effective request"):
        owner.diagnostics.validate_report(report)
    await owner.inspect_run_diagnostic(run_id=42, section="request", turn=1, offset=100)
    owner.diagnostics.validate_report(report)


async def test_fallback_is_explicitly_inspect_only(owner, monkeypatch):
    from api.services.configuration import model_connections

    config = SimpleNamespace(
        llm=SimpleNamespace(provider="openai", model="frozen"),
        llm_fallback=SimpleNamespace(rules=[object()]),
    )
    monkeypatch.setattr(
        model_connections,
        "hydrate_model_configuration_snapshot",
        AsyncMock(return_value=config),
    )
    await owner.start_diagnosis("Why?", [42])
    with pytest.raises(WorkbenchError, match="fallback route"):
        await owner.run_diagnostic_probe(
            run_id=42, turn=1, hypothesis="h", prediction="p", node_id="n"
        )
    assert owner.diagnostics.requests == 0


async def test_resume_reuses_completed_probe_only_for_matching_frozen_evidence(
    owner, packet, monkeypatch
):
    probe = {
        "evidence_id": "probe:old",
        "run_id": 42,
        "turn": 1,
        "job_id": "old-job",
        "definition_fingerprint": d.fingerprint(packet["definition"]["workflow_json"]),
        "model_fingerprint": d.fingerprint(
            packet["run"]["model_configuration_snapshot"]
        ),
        "results": [{"arm": "baseline", "text": "observed"}],
        "baseline": {},
        "variant": {},
    }
    mismatch = {
        **probe,
        "evidence_id": "probe:stale",
        "definition_fingerprint": "different",
    }
    monkeypatch.setattr(
        d.db_client, "wb_diagnostic_probes", AsyncMock(return_value=[probe, mismatch])
    )
    result = await owner.start_diagnosis("Continue why?", [42])
    assert len(result["previous_probes"]) == 1
    assert result["previous_probes"][0]["evidence_id"] == "probe:old"
    assert result["previous_probes"][0]["source_job_id"] == "old-job"
    assert owner.diagnostics.requests == 0
    await owner.start_diagnosis("Continue why?", [42])
    assert len(owner.diagnostics.experiments) == 1


async def test_capture_sink_failure_never_changes_model_execution():
    service = SimpleNamespace(
        _client=SimpleNamespace(
            api_key="secret", _client=SimpleNamespace(event_hooks={})
        )
    )
    engine = SimpleNamespace(active_agent=SimpleNamespace(current_node=None))
    emit = Mock(side_effect=RuntimeError("sink unavailable"))
    assert d.install_request_capture(service, engine, emit)
    request = httpx.Request(
        "POST", "https://example.test/v1/chat/completions", json={"messages": []}
    )
    await service._client._client.event_hooks["request"][0](request)
    assert json.loads(request.content) == {"messages": []}


async def test_independent_reviewer_can_read_sources_but_cannot_probe_or_edit(owner):
    await owner.start_diagnosis("Why?", [42])
    reviewer = d.DiagnosticReviewer(owner)
    assert {s["name"] for s in reviewer.specs()} == {
        "read_task_basis",
        "inspect_run_diagnostic",
    }
    owner.read_task_basis = AsyncMock(return_value={"text": "Original requirement"})
    assert await reviewer.call("read_task_basis", {"source_id": "source:1"}) == {
        "text": "Original requirement"
    }
    result = await reviewer.call(
        "inspect_run_diagnostic", {"run_id": 42, "section": "request", "turn": 1}
    )
    assert result["provenance"] == "reconstructed"
    with pytest.raises(WorkbenchError, match="outside this diagnosis"):
        await reviewer.call("inspect_run_diagnostic", {"run_id": 100})
    with pytest.raises(WorkbenchError, match="evidence reads only"):
        await reviewer.call("run_diagnostic_probe", {"run_id": 42})


async def test_real_app_server_accepts_verifier_with_evidence_reader(
    owner, tmp_path, monkeypatch
):
    import os

    from aiohttp import web

    from api.services.agent_workbench.codex_client import CodexClient
    from api.services.agent_workbench.test_tasks import StructuredDecision

    if os.getenv("RUN_CODEX_SOURCE_TESTS") != "1":
        pytest.skip("Set RUN_CODEX_SOURCE_TESTS=1 for the actual app-server contract")
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "jobs"))
    seen = []

    async def upstream(request):
        seen.append(await request.json())
        return web.json_response(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_verify",
                        "call_id": "call_verify",
                        "name": "verify_analysis",
                        "arguments": '{"supported":true}',
                        "status": "completed",
                    }
                ],
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    dispatcher = StructuredDecision(
        "verify_analysis",
        "Verify sources",
        {
            "type": "object",
            "properties": {"supported": {"type": "boolean"}},
            "required": ["supported"],
            "additionalProperties": False,
        },
        reader=d.DiagnosticReviewer(owner),
    )
    assert len({t.get("type") for t in dispatcher.specs()}) == 1
    try:
        await CodexClient(dispatcher, AsyncMock()).run(
            "Verify the supplied evidence.",
            SimpleNamespace(
                model="test-model",
                api_key="local-test-key",
                base_url=f"http://127.0.0.1:{port}/v1",
            ),
            protocol="responses",
        )
        assert dispatcher.report == {"supported": True}
        assert {t["name"] for t in seen[0]["tools"]} == {
            "verify_analysis",
            "read_task_basis",
            "inspect_run_diagnostic",
        }
    finally:
        await runner.cleanup()


async def test_original_sources_are_citable_only_after_reading_their_pages(owner):
    await owner.start_diagnosis("Why?", [42])
    await owner.inspect_run_diagnostic(run_id=42, section="request", turn=1)
    report = {
        "status": "concluded",
        "findings": [
            {
                "claim": "Source requirement",
                "status": "supported",
                "evidence_ids": ["request:original"],
            }
        ],
    }
    with pytest.raises(WorkbenchError, match="cite inspected"):
        owner.diagnostics.validate_report(report)
    source = {
        "id": "request:original",
        "title": "Original",
        "text": "Requirement",
        "offset": 0,
        "next_offset": None,
    }
    owner.diagnostics.record_source(source)
    owner.diagnostics.validate_report(report)
    assert owner.diagnostics.reads["request:original"]["content"]["pages"] == [source]

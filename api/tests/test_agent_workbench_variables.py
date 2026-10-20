"""Candidate Variables must follow the graph through build, tune and adoption."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import jsonschema
import pytest

from api.services.agent_workbench import tools as module
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.variables import (
    candidate_variables,
    referenced_variables,
    variable_report,
)

CODE = """
import { Workflow } from "@dograh/sdk";
import { startCall, endCall } from "@dograh/sdk/typed";
const wf = new Workflow({ name: "Variables agent" });
const start = wf.addTyped(startCall({
  name: "Greeting", prompt: "Help {{customer}} at {{company}}. {{current_weekday}}"
}));
const end = wf.addTyped(endCall({ name: "Done", prompt: "Goodbye." }));
wf.edge(start, end, { label: "done", condition: "User is finished" });
"""


def test_references_match_runtime_semantics_and_cover_edges():
    graph = {
        "nodes": [
            {
                "data": {
                    "prompt": "{{company}} {{ current_time_Asia/Shanghai }} "
                    "{{current_weekday}} {{current_weekday_Asia/Shanghai}} "
                    "{{gathered_context.city}} {{optional | fallback:hello}} "
                    "{{provider}} {{campaign_id}} {{source_uuid}}",
                    "greeting": "{{customer}}",
                    "nested": ["{{company}}", "{{岗位}}"],
                }
            }
        ],
        "edges": [{"data": {"transition_speech": "{{address}}"}}],
    }
    assert referenced_variables(graph) == ["address", "company", "customer", "岗位"]
    existing = {"company": "Real company", "unused": "Keep this"}
    result = candidate_variables(graph, existing, {"address": "Provided address"})
    assert result == {
        **existing,
        "address": "Provided address",
        "customer": "",
        "岗位": "",
    }
    assert existing == {"company": "Real company", "unused": "Keep this"}
    assert variable_report(
        {"workflow_json": graph, "template_context_variables": result}
    )["missing_variables"] == ["customer", "岗位"]


@pytest.mark.parametrize(
    "updates",
    [{"bad.key": "x"}, {"company": None}, {"": "value"}, {"company": "x" * 20001}],
)
def test_rejects_invalid_default_updates(updates):
    with pytest.raises(jsonschema.ValidationError):
        candidate_variables({}, {}, updates)


@pytest.mark.parametrize(
    "name", ["current_time", "current_weekday_Asia/Shanghai", "provider"]
)
def test_rejects_overriding_runtime_variables(name):
    with pytest.raises(WorkbenchError, match="runtime"):
        candidate_variables({}, {}, {name: "A frozen value"})


@pytest.mark.parametrize("tuning", [False, True])
async def test_sdk_tools_register_defaults_and_keep_tuning_baseline(
    monkeypatch, tuning
):
    for method in (
        "get_tools_for_organization",
        "get_documents_for_organization",
        "get_credentials_for_organization",
        "get_recordings",
    ):
        monkeypatch.setattr(module.db_client, method, AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "prepare_models", AsyncMock())
    saved = AsyncMock(return_value={"id": "candidate", "fingerprint": "new"})
    monkeypatch.setattr(module.db_client, "wb_save_candidate", saved)
    baseline = {
        "name": "Existing",
        "workflow_json": {"nodes": [], "edges": []},
        "workflow_configurations": {},
        "template_context_variables": {"company": "Original", "keep": "Unchanged"},
    }
    tools = module.WorkbenchTools(
        1,
        "session",
        {"id": "job", "lease_token": "lease"},
        {"target": baseline, "target_fingerprint": "base"},
        tuning=tuning,
    )
    result = await tools.validate_candidate(CODE, {"company": "From the request"})
    assert result["missing_variables"] == ["customer"]
    assert result["template_context_variables"] == {
        "company": "From the request",
        "keep": "Unchanged",
        "customer": "",
    }
    saved_result = await tools.save_candidate(
        CODE, "Added variables", {"company": "From the request"}
    )
    snapshot = saved.call_args.args[4]
    assert (
        snapshot["template_context_variables"] == result["template_context_variables"]
    )
    assert saved_result["missing_variables"] == ["customer"]
    assert baseline["template_context_variables"] == {
        "company": "Original",
        "keep": "Unchanged",
    }
    assert tools.candidate_ids == ["candidate"]
    # Old callers and tuning that only change prompts keep existing defaults.
    assert (await tools.validate_candidate(CODE))["template_context_variables"][
        "company"
    ] == "Original"


async def test_simulation_uses_candidate_defaults_and_case_overrides(monkeypatch):
    from api.services.agent_workbench import evaluation
    from api.services.workflow import text_chat_runner

    defaults = {"company": "Provided", "customer": "", "keep": "Retained"}
    suite = {
        "name": "Variables",
        "cases": [
            {
                "name": "Per-contact data",
                "user_messages": ["bye"],
                "initial_context": {
                    "customer": "Test contact",
                    "company": "Case override",
                },
                "assertions": [{"kind": "contains", "value": "hello"}],
            }
        ],
    }

    async def get(_org, _sid, table, _id):
        return (
            {
                "snapshot": {
                    "workflow_json": {"nodes": []},
                    "template_context_variables": defaults,
                },
                "fingerprint": "c",
            }
            if table == "candidates"
            else {"payload": suite, "fingerprint": "s"}
        )

    monkeypatch.setattr(evaluation.db_client, "wb_get", get)
    for method, value in [
        ("wb_context", {"session": {"workflow_id": 1}}),
        ("wb_evaluation", {"id": "evaluation"}),
        ("wb_create_run", 1),
        ("wb_heartbeat", None),
        ("wb_evaluation_result", None),
        ("update_workflow_run", None),
    ]:
        monkeypatch.setattr(evaluation.db_client, method, AsyncMock(return_value=value))
    monkeypatch.setattr(
        text_chat_runner,
        "execute_text_chat_pending_turn",
        AsyncMock(
            return_value=SimpleNamespace(
                checkpoint={},
                gathered_context={},
                is_completed=True,
                assistant_text="hello",
                events=[],
                usage={},
            )
        ),
    )
    result = await evaluation.evaluate(
        1,
        "s",
        {"id": "j", "lease_token": "l"},
        "c",
        "suite",
        SimpleNamespace(llm=SimpleNamespace(model="test")),
    )
    assert result["summary"]["passed"] == 1
    assert evaluation.db_client.wb_create_run.call_args.args[-1] == {
        "company": "Case override",
        "customer": "Test contact",
        "keep": "Retained",
    }
    assert defaults == {"company": "Provided", "customer": "", "keep": "Retained"}

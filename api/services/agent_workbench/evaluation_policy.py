"""Fail-closed, server-only effect policy for the real text runtime."""

import asyncio
import copy

from api.services.agent_workbench.assertions import json_equal, state_value

import jsonschema
from pipecat.adapters.schemas.function_schema import FunctionSchema

from api.services.agent_workbench.errors import WorkbenchError


class EvaluationPolicy:
    def __init__(self, case, model_configuration):
        self.case, self.model_configuration = case, model_configuration
        self.calls, self.errors = [], []
        self.requests = []
        self.fixtures = {fixture.name: fixture for fixture in case.fixtures}
        self.business_state = copy.deepcopy(case.initial_business_state)
        self._attempts = {}
        self.tool_definitions = {}

    def validate(self, graph):
        supported = {"startCall", "endCall", "agentNode", "globalNode", "trigger"}
        fixtures = {fixture.tool_uuid for fixture in self.case.fixtures}
        for node in graph.get("nodes", []):
            # Post-call integrations are never run; reject other unknown live node types.
            if node.get("type") not in supported | {
                "qa",
                "noveum",
                "paygent",
                "tuner",
                "webhook",
            }:
                raise WorkbenchError("Unsupported simulation node type")
            data = node.get("data", {})
            if not set(data.get("tool_uuids") or []) <= fixtures:
                raise WorkbenchError(
                    "Simulation requires fixtures for every referenced tool, including MCP"
                )
            if data.get("pre_call_fetch_url") and self.case.pre_call_context is None:
                raise WorkbenchError(
                    "Simulation requires pre_call_context for pre-call HTTP fetch"
                )
            if (
                data.get("document_uuids")
                and self.case.knowledge_result is None
                and not self.case.knowledge_chunks
            ):
                raise WorkbenchError("Simulation requires a knowledge_result fixture")
            if data.get("greeting_recording_id"):
                raise WorkbenchError(
                    "Use a text greeting for text simulation; audio recordings are not evaluated"
                )
        for edge in graph.get("edges", []):
            if (edge.get("data") or {}).get("transition_speech_recording_id"):
                raise WorkbenchError(
                    "Recorded edge speech is not supported by text simulation"
                )

    def selected(self, tool_uuids, filters=None):
        return [
            fixture
            for fixture in self.fixtures.values()
            if fixture.tool_uuid in tool_uuids
            and (
                filters is None
                or fixture.tool_uuid not in filters
                or fixture.name in filters[fixture.tool_uuid]
            )
        ]

    def schemas(self, tool_uuids, filters=None):
        return [
            FunctionSchema(
                name=f.name,
                description=f.description,
                properties=f.parameters.get("properties", {}),
                required=f.parameters.get("required", []),
            )
            for f in self.selected(tool_uuids, filters)
        ]

    def invoke(self, name, arguments):
        result, _ = self._invoke(name, arguments)
        return result

    def _invoke(self, name, arguments):
        call = {"name": name, "arguments": copy.deepcopy(arguments)}
        self.calls.append(call)
        fixture = self.fixtures.get(name)
        if fixture is None:
            self.errors.append("No fixture for " + name)
            call["result"] = {"error": "fixture_missing"}
            return call["result"], 0
        try:
            jsonschema.validate(arguments, fixture.parameters)
            if fixture.arguments is not None and not json_equal(arguments, fixture.arguments):
                raise WorkbenchError("Fixture arguments did not match for " + name)
        except (jsonschema.ValidationError, WorkbenchError) as error:
            self.errors.append(str(error)[:1000])
            call["result"] = {"error": "fixture_arguments_mismatch"}
            return call["result"], 0
        index = self._attempts.get(name, 0)
        self._attempts[name] = index + 1
        delay = 0
        if fixture.steps:
            if index >= len(fixture.steps):
                self.errors.append("Fixture steps exhausted for " + name)
                call["result"] = {"error": "fixture_steps_exhausted"}
                return call["result"], 0
            step = fixture.steps[index]
            self.business_state.update(copy.deepcopy(step.state_patch))
            result = copy.deepcopy(step.result)
            if step.outcome != "success":
                result = {"status": step.outcome, "result": result}
            call["outcome"] = step.outcome
            delay = step.delay_ms
        else:
            result = copy.deepcopy(fixture.result)
        if fixture.state_result_key:
            found, value = state_value(self.business_state, fixture.state_result_key)
            result = (
                copy.deepcopy(value) if found else {"error": "fixture_state_missing"}
            )
        call["result"] = copy.deepcopy(result)
        call["delay_ms"] = delay
        return result, delay

    async def invoke_async(self, name, arguments):
        result, delay = self._invoke(name, arguments)
        if delay:
            await asyncio.sleep(delay / 1000)
        return result

    def register(self, agent, engine, tool_uuids, filters=None):
        for fixture in self.selected(tool_uuids, filters):

            async def callback(params, name=fixture.name):
                from types import SimpleNamespace
                from api.services.workflow.action_guard import execute_guarded_http

                selected = self.fixtures[name]

                async def fixture_executor(**kwargs):
                    matching = next(
                        (
                            f
                            for f in self.fixtures.values()
                            if f.tool_uuid == kwargs["tool"].tool_uuid
                        ),
                        None,
                    )
                    if matching is None:
                        self.errors.append("No fixture for reconciliation tool")
                        return {"status": "error", "error": "fixture_missing"}
                    return await self.invoke_async(matching.name, kwargs["arguments"])

                definition = self.tool_definitions.get(
                    selected.tool_uuid, {"type": "http_api", "config": {}}
                )
                result = await execute_guarded_http(
                    engine=engine,
                    tool=SimpleNamespace(
                        name=name,
                        description=selected.description,
                        tool_uuid=selected.tool_uuid,
                        definition=definition,
                    ),
                    arguments=params.arguments,
                    organization_id=await engine._get_organization_id(),
                    tool_call_id=params.tool_call_id,
                    executor=fixture_executor,
                    schema_override=selected.parameters
                    if selected.tool_uuid not in self.tool_definitions
                    else None,
                )
                self.requests.append(
                    {
                        "name": name,
                        "arguments": copy.deepcopy(params.arguments),
                        "result": copy.deepcopy(result),
                    }
                )
                await params.result_callback(result)

            agent.llm.register_function(fixture.name, agent.bind_tool(engine, callback))

    def knowledge(self):
        if self.case.knowledge_result is None and not self.case.knowledge_chunks:
            self.errors.append("Knowledge fixture missing")
            return {"error": "fixture_missing", "chunks": []}
        self.calls.append({"name": "retrieve_from_knowledge_base"})
        chunks = (
            copy.deepcopy(self.case.knowledge_chunks)
            if self.case.knowledge_chunks
            else [{"content": self.case.knowledge_result}]
        )
        return {"chunks": chunks, "total_results": len(chunks)}

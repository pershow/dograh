"""Fail-closed, server-only effect policy for the real text runtime."""

import copy

import jsonschema
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.utils.errors import ErrorCategory

from api.errors.failure import redact_failure_message
from api.services.agent_workbench.errors import WorkbenchError


class EvaluationPolicy:
    def __init__(self, case, model_configuration):
        self.case, self.model_configuration = case, model_configuration
        self.calls, self.errors = [], []
        self.fixtures = {fixture.name: fixture for fixture in case.fixtures}

    def record_runtime_error(self, frame):
        """Keep the actionable provider cause, without credentials or unbounded text."""
        message = str(getattr(frame, "error", "Model or tool request failed"))
        config = getattr(self.model_configuration, "llm", None)
        secret = getattr(config, "api_key", None)
        if hasattr(secret, "get_secret_value"):
            secret = secret.get_secret_value()
        if secret:
            message = message.replace(str(secret), "[REDACTED]")
        self.errors.append(
            "文本运行时调用失败：" + redact_failure_message(message)[:1000]
        )
        # Provider categories are independent of customer wording and business
        # verdicts. Fixture/validation errors must not trip model availability.
        if getattr(frame, "category", None) in {
            ErrorCategory.AUTHENTICATION,
            ErrorCategory.AUTHORIZATION,
            ErrorCategory.QUOTA,
            ErrorCategory.RATE_LIMIT,
            ErrorCategory.CONNECTIVITY,
            ErrorCategory.SERVER,
        }:
            from api.services.agent_workbench.model_transport import (
                record_model_outcome,
            )

            record_model_outcome(
                config, "model_connection_error", detail=self.errors[-1]
            )

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
            if data.get("document_uuids") and self.case.knowledge_result is None:
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
        fixture = self.fixtures.get(name)
        if fixture is None:
            self.errors.append("No fixture for " + name)
            return {"error": "fixture_missing"}
        try:
            jsonschema.validate(arguments, fixture.parameters)
            if fixture.arguments is not None and arguments != fixture.arguments:
                raise WorkbenchError("Fixture arguments did not match for " + name)
        except (jsonschema.ValidationError, WorkbenchError) as error:
            self.errors.append(str(error)[:1000])
            return {"error": "fixture_arguments_mismatch"}
        self.calls.append(
            {
                "name": name,
                "arguments": copy.deepcopy(arguments),
                "result": copy.deepcopy(fixture.result),
            }
        )
        return copy.deepcopy(fixture.result)

    def register(self, agent, engine, tool_uuids, filters=None):
        for fixture in self.selected(tool_uuids, filters):

            async def callback(params, name=fixture.name):
                await params.result_callback(self.invoke(name, params.arguments))

            agent.llm.register_function(fixture.name, agent.bind_tool(engine, callback))

    def knowledge(self):
        if self.case.knowledge_result is None:
            self.errors.append("Knowledge fixture missing")
            return {"error": "fixture_missing", "chunks": []}
        self.calls.append({"name": "retrieve_from_knowledge_base"})
        return {"chunks": [{"content": self.case.knowledge_result}], "total_results": 1}

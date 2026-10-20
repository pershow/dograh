"""Public, bounded contracts for the agent workbench (never executable test code)."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSession(StrictModel):
    model_protocol: Literal["chat", "responses"] = "chat"
    workflow_id: int | None = None
    name: str = Field(default="New Agent", min_length=1, max_length=120)


class SessionSettings(StrictModel):
    model_protocol: Literal["chat", "responses"]


class Instruction(StrictModel):
    candidate_id: str | None = None
    mode: Literal["auto", "analyze", "build"] = "auto"
    text: str = Field(min_length=1, max_length=24000)
    asset_ids: list[str] = Field(default_factory=list, max_length=5)
    idempotency_key: str = Field(min_length=1, max_length=128)


class Assertion(StrictModel):
    """node_visited.value accepts a node ID or an exact, unique node name."""

    kind: Literal[
        "contains",
        "not_contains",
        "node_visited",
        "tool_called",
        "context_equals",
        "completed",
    ]
    value: Any = None
    key: str | None = None

    @model_validator(mode="after")
    def validate_assertion(self):
        if self.kind in {
            "contains",
            "not_contains",
            "node_visited",
            "tool_called",
        } and (not isinstance(self.value, str) or not self.value):
            raise ValueError("This assertion requires a nonempty string value")
        if (
            self.kind == "completed"
            and self.value is not None
            and not isinstance(self.value, bool)
        ):
            raise ValueError("completed requires a boolean")
        if self.kind == "context_equals" and not self.key:
            raise ValueError("context_equals requires a key")
        return self


class ToolFixture(StrictModel):
    tool_uuid: str
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="Simulation fixture", max_length=1000)
    parameters: dict = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    arguments: dict | None = None
    result: Any = None

    @model_validator(mode="after")
    def validate_parameters(self):
        import jsonschema

        def walk(value):
            if isinstance(value, dict):
                if any(key in value for key in ("$ref", "$dynamicRef", "$id")):
                    raise ValueError(
                        "Fixture schemas must be inline and cannot reference remote resources"
                    )
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(self.parameters)
        try:
            jsonschema.Draft202012Validator.check_schema(self.parameters)
        except jsonschema.SchemaError as error:
            raise ValueError("Invalid fixture parameter schema") from error
        return self


class SimulationCase(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    user_messages: list[str] = Field(min_length=1, max_length=12)
    initial_context: dict = Field(default_factory=dict)
    fixtures: list[ToolFixture] = Field(default_factory=list, max_length=50)
    pre_call_context: dict | None = None
    knowledge_result: str | None = Field(default=None, max_length=24000)
    assertions: list[Assertion] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def bounded_messages(self):
        if any(len(message) > 4000 for message in self.user_messages):
            raise ValueError("Each simulated message must be at most 4000 characters")
        if len({f.name for f in self.fixtures}) != len(self.fixtures):
            raise ValueError("Fixture function names must be unique")
        return self


class SimulationSuite(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    cases: list[SimulationCase] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def bound_payload(self):
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode()) > 256000:
            raise ValueError("Simulation suite exceeds 256 KB")
        return self


class EvaluateRequest(StrictModel):
    candidate_id: str
    suite_id: str
    idempotency_key: str = Field(min_length=1, max_length=128)


class TuneRequest(EvaluateRequest):
    asset_ids: list[str] = Field(default_factory=list, max_length=5)
    instruction: str = Field(
        default="Fix the failing cases without regressing passing cases.",
        max_length=12000,
    )
    max_rounds: int = Field(default=1, ge=1, le=3)


class VoiceSelection(StrictModel):
    candidate_id: str | None = None
    suite_id: str | None = None
    asset_ids: list[str] = Field(default_factory=list, max_length=5)


class VoiceToolRequest(VoiceSelection):
    call_id: str = Field(min_length=1, max_length=128)
    name: Literal[
        "submit_instruction",
        "get_status",
        "read_attachment",
        "cancel_task",
        "run_tests",
        "tune_agent",
        "list_agent_runs",
        "read_agent_run",
        "read_conversation",
    ]
    arguments: dict = Field(default_factory=dict)


class WorkbenchResponse(BaseModel):
    """JSON envelope shared by the generated SDK; domain payloads are versioned."""

    data: dict[str, Any]

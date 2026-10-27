"""Public, bounded contracts for the agent workbench (never executable test code)."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.schemas.model_connections import ServiceSelection


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSession(StrictModel):
    new_session: bool = False
    model_protocol: Literal["chat", "responses"] = "chat"
    workflow_id: int | None = None
    name: str = Field(default="New Agent", min_length=1, max_length=120)


class SessionSettings(StrictModel):
    model_protocol: Literal["chat", "responses"]


class BuildModelSettings(StrictModel):
    llm: ServiceSelection | None


class BuildModelSettingsResponse(BuildModelSettings):
    supported_providers: list[str]


class ConfirmMemory(StrictModel):
    kind: Literal["requirement", "constraint", "sop"]
    content: str = Field(min_length=1, max_length=200000)
    expected_revision: str
    replaces_id: str | None = None
    source_type: Literal["request", "attachment"] = "request"
    source_id: str | None = None
    source_quote: str | None = Field(default=None, max_length=200000)


class RevokeMemory(StrictModel):
    expected_revision: str


class Instruction(StrictModel):
    test_suite_ids: list[str] = Field(default_factory=list, max_length=100)
    test_mode: Literal["scripted", "single_step", "dynamic"] | None = None
    single_step_turns: list[int] = Field(default_factory=list, max_length=400)
    candidate_id: str | None = None
    test_target_session_id: str | None = None
    continue_test_job_id: str | None = None
    resume_build_job_id: str | None = None
    rerun_test_job_id: str | None = None
    review_evaluation_ids: list[str] = Field(default_factory=list, max_length=100)
    mode: Literal["auto", "analyze", "build", "test"] = "auto"
    text: str = Field(min_length=1, max_length=24000)
    asset_ids: list[str] = Field(default_factory=list, max_length=5)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def one_test_operation(self):
        if self.resume_build_job_id and (
            self.mode != "build" or self.candidate_id or self.test_target_session_id
            or self.continue_test_job_id or self.rerun_test_job_id
            or self.review_evaluation_ids or self.test_suite_ids or self.asset_ids
        ):
            raise ValueError("Resume build must use the original candidate, tests and request")
        if len(set(self.test_suite_ids)) != len(self.test_suite_ids):
            raise ValueError("Test suites must not be duplicated")
        if any(n < 1 for n in self.single_step_turns) or len(
            set(self.single_step_turns)
        ) != len(self.single_step_turns):
            raise ValueError("Single-step turn numbers must be positive and unique")
        if self.review_evaluation_ids and (
            self.mode != "test" or self.continue_test_job_id or self.rerun_test_job_id
        ):
            raise ValueError(
                "Review existing conversations requires test mode and cannot be combined with replay/resume"
            )
        if len(set(self.review_evaluation_ids)) != len(self.review_evaluation_ids):
            raise ValueError("Evaluation IDs must be unique")
        if self.test_target_session_id and (
            self.mode != "test" or not self.candidate_id
        ):
            raise ValueError(
                "A cross-session target is only allowed for read-only tests with a candidate"
            )
        if self.continue_test_job_id and self.rerun_test_job_id:
            raise ValueError("Choose either resume or fresh rerun, not both")
        return self


class Assertion(StrictModel):
    """node_visited.value accepts a node ID or an exact, unique node name."""

    kind: Literal[
        "contains",
        "contains_any",
        "not_contains",
        "node_visited",
        "node_not_visited",
        "node_sequence",
        "tool_called",
        "context_equals",
        "completed",
    ]
    value: Any = None
    key: str | None = None
    turn: int | None = Field(
        default=None,
        ge=0,
        description="For text checks, 0 is the opening; 1 is the reply to the first user message. Omit to check all assistant speech.",
    )

    @model_validator(mode="after")
    def validate_assertion(self):
        if self.kind in {
            "contains",
            "not_contains",
            "node_visited",
            "node_not_visited",
            "tool_called",
        } and (not isinstance(self.value, str) or not self.value):
            raise ValueError("This assertion requires a nonempty string value")
        if self.kind in {"contains_any", "node_sequence"} and (
            not isinstance(self.value, list)
            or not self.value
            or any(not isinstance(v, str) or not v for v in self.value)
        ):
            raise ValueError("This assertion requires a nonempty list of strings")
        if self.turn is not None and self.kind not in {
            "contains",
            "contains_any",
            "not_contains",
        }:
            raise ValueError("turn is only supported for text assertions")
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


class DialogueTurn(StrictModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=100000)


class SimulationCase(StrictModel):
    mode: Literal["scripted", "single_step", "dynamic"] = "scripted"
    reference_turns: list[DialogueTurn] = Field(default_factory=list, max_length=1000)
    history: list[DialogueTurn] = Field(default_factory=list, max_length=1000)
    source_turn: int | None = Field(default=None, ge=1)
    max_turns: int = Field(default=80, ge=1, le=400)
    name: str = Field(min_length=1, max_length=120)
    user_messages: list[str] = Field(
        min_length=1,
        max_length=400,
        description="Only the simulated USER's utterances, in order. Never include assistant greetings or replies; the runtime generates those.",
    )
    initial_context: dict = Field(default_factory=dict)
    fixtures: list[ToolFixture] = Field(default_factory=list, max_length=50)
    pre_call_context: dict | None = None
    knowledge_result: str | None = Field(default=None, max_length=24000)
    assertions: list[Assertion] = Field(default_factory=list, max_length=50)
    source: dict = Field(default_factory=dict)
    reference_dialogue: str | None = Field(default=None, max_length=100000)

    @model_validator(mode="after")
    def bounded_messages(self):
        if self.mode == "single_step" and (
            len(self.user_messages) != 1 or not self.history
        ):
            raise ValueError(
                "Single-step tests require the original history and exactly one customer message"
            )
        if self.mode == "dynamic" and not self.reference_turns:
            raise ValueError("Dynamic tests require a structured source conversation")
        if not self.assertions and not self.reference_dialogue:
            raise ValueError("Tests need assertions or a reference dialogue")
        if any(len(message) > 4000 for message in self.user_messages):
            raise ValueError("Each simulated message must be at most 4000 characters")
        if len({f.name for f in self.fixtures}) != len(self.fixtures):
            raise ValueError("Fixture function names must be unique")
        if any(
            a.turn is not None and a.turn > len(self.user_messages)
            for a in self.assertions
        ):
            raise ValueError(
                f"Assertion turn must be between 0 and {len(self.user_messages)}: 0 is the opening, 1 is the response to user_messages[0], 2 to user_messages[1]. Do not use alternating transcript indices."
            )
        return self


class SimulationSuite(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    instructions: str = Field(default="", max_length=50000)
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

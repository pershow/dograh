"""Versioned, bounded business and interaction contracts for an Agent."""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class FactDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
    description: str = Field(default="", max_length=1000)
    value_type: Literal["string", "number", "boolean"] = "string"


class TaskStage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    required_facts: list[str] = Field(default_factory=list, max_length=50)
    required_actions: list[str] = Field(default_factory=list, max_length=50)
    terminal: bool = False
    outcome: Literal["success", "failure"] = "success"


class KnowledgeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Literal["knowledge", "policy", "example"] = "knowledge"
    version: str | None = Field(default=None, min_length=64, max_length=64)
    applies_when: dict[str, str | int | float | bool] = Field(
        default_factory=dict, max_length=50
    )


class AgentBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    enabled: bool = False
    goal_type: Literal["service", "sales", "custom"] = "custom"
    goal: str = Field(default="", max_length=2000)
    success_criteria: list[str] = Field(default_factory=list, max_length=20)
    stop_criteria: list[str] = Field(default_factory=list, max_length=20)
    facts: list[FactDefinition] = Field(default_factory=list, max_length=50)
    stages: list[TaskStage] = Field(default_factory=list, max_length=30)
    reply_style: Literal["concise", "balanced", "detailed"] = "concise"
    listen_during_tools: bool = False
    tool_policies: dict[str, "ToolExecutionPolicy"] = Field(
        default_factory=dict, max_length=50
    )
    knowledge_bindings: dict[str, KnowledgeBinding] = Field(
        default_factory=dict, max_length=100
    )
    confirmation_phrases: list[str] = Field(
        default_factory=lambda: ["确认", "确认执行", "confirm"],
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def references(self):
        names = {fact.name for fact in self.facts}
        if len(names) != len(self.facts) or len(
            {stage.name for stage in self.stages}
        ) != len(self.stages):
            raise ValueError("Fact and stage names must be unique")
        if any(set(stage.required_facts) - names for stage in self.stages):
            raise ValueError("Stage references an undeclared fact")
        if any(
            set(stage.required_actions) - set(self.tool_policies)
            for stage in self.stages
        ):
            raise ValueError("Stage references an undeclared tool policy")
        if any(
            self.tool_policies[name].effect != "write"
            for stage in self.stages
            for name in stage.required_actions
        ):
            raise ValueError("Stage completion requires write action outcomes")
        if any(
            set(policy.required_facts) - names for policy in self.tool_policies.values()
        ):
            raise ValueError("Tool policy references an undeclared fact")
        if any(
            set(binding.applies_when) - names
            for binding in self.knowledge_bindings.values()
        ):
            raise ValueError("Knowledge applicability references an undeclared fact")
        if any(
            not phrase.strip() or len(phrase) > 100
            for phrase in self.confirmation_phrases
        ):
            raise ValueError("Confirmation phrases must contain 1–100 characters")
        if any(
            len(text) > 2000 for text in [*self.success_criteria, *self.stop_criteria]
        ):
            raise ValueError("Criteria must contain at most 2000 characters")
        return self


class ToolExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    effect: Literal["read", "write"]
    required_facts: list[str] = Field(default_factory=list, max_length=50)
    identity_arguments: list[str] = Field(default_factory=list, max_length=20)
    confirmation_required: bool = True
    confirmation_template: str = Field(default="", max_length=2000)
    success_path: str = Field(default="", max_length=255)
    success_equals: Any = True
    failure_path: str | None = Field(default=None, max_length=255)
    failure_equals: Any = False
    idempotency_header: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9-]{1,100}$"
    )
    reconcile_tool_uuid: str | None = Field(default=None, max_length=100)
    reconcile_arguments: dict[str, str] = Field(
        default_factory=lambda: {"operation_id": "operation_id"}, max_length=20
    )
    lease_seconds: int = Field(default=60, ge=10, le=600)

    @model_validator(mode="after")
    def validate_write(self):
        if self.effect == "write" and not self.success_path.startswith("data."):
            raise ValueError("Write tools require a business success field below data")
        if self.failure_path and not self.failure_path.startswith("data."):
            raise ValueError("Business failure fields must be below data")
        if self.idempotency_header and self.idempotency_header.lower() in {
            "authorization",
            "cookie",
            "host",
            "content-type",
            "content-length",
        }:
            raise ValueError(
                "Idempotency headers cannot replace authentication or transport headers"
            )
        return self


AgentBehavior.model_rebuild()

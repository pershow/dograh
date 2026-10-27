"""Run-local facts and task checkpoints with explicit evidence and revisions."""

import copy
import json
import math
import re
import uuid

from pipecat.adapters.schemas.function_schema import FunctionSchema

from api.schemas.agent_behavior import AgentBehavior


def normalize_confirmation(text):
    return re.sub(r"[\s。.!！?？,，]+", "", text).casefold()


class TaskState:
    def __init__(self, contract: AgentBehavior, checkpoint=None):
        self.contract = contract
        self.data = (
            copy.deepcopy(checkpoint)
            if checkpoint
            else {
                "version": 1,
                "task_id": str(uuid.uuid4()),
                "revision": 0,
                "turn": 0,
                "last_user_text": "",
                "facts": {},
                "stage": None,
                "status": "active",
                "last_task_turn": 0,
            }
        )
        self.data.setdefault("input_revision", self.data["turn"])
        self.data.setdefault("text_revision", self.data["turn"])

    def observe_user(self, text):
        self.observe_activity()
        self.data["turn"] += 1
        self.data["last_user_text"] = text
        self.data["text_revision"] = self.data["input_revision"]

    def observe_activity(self):
        self.data["input_revision"] = (
            self.data.get("input_revision", self.data["turn"]) + 1
        )

    def confirmation(self):
        if self.data.get("text_revision") != self.data.get("input_revision"):
            return False
        value = normalize_confirmation(self.data["last_user_text"])
        return bool(value) and value in {
            normalize_confirmation(p) for p in self.contract.confirmation_phrases
        }

    def missing(self, names):
        return [
            name
            for name in names
            if self.data["facts"].get(name, {}).get("value") is None
        ]

    def update(self, *, facts=None, stage=None, new_task=False, quote="", actions=None):
        if self.data.get("text_revision") != self.data.get("input_revision"):
            return {
                "status": "rejected",
                "reason": "Wait for the current user turn to finish",
            }
        if not quote.strip() or quote not in self.data["last_user_text"]:
            return {
                "status": "rejected",
                "reason": "An exact quote from the current user turn is required",
            }
        if new_task:
            if (
                self.data["status"] not in {"completed", "failed"}
                or self.data["turn"] <= self.data["last_task_turn"]
            ):
                return {
                    "status": "rejected",
                    "reason": "Finish the current task and wait for a new user request",
                }
        definitions = {f.name: f for f in self.contract.facts}
        proposed = {} if new_task else copy.deepcopy(self.data["facts"])
        for name, value in (facts or {}).items():
            definition = definitions.get(name)
            if definition is None:
                return {"status": "rejected", "reason": "Undeclared fact: " + name}
            correct = (
                value is None
                or {
                    "string": isinstance(value, str)
                    and bool(value.strip())
                    and len(value) <= 4000,
                    "number": type(value) in (int, float) and math.isfinite(value),
                    "boolean": isinstance(value, bool),
                }[definition.value_type]
            )
            if not correct:
                return {"status": "rejected", "reason": "Invalid fact type: " + name}
            previous = proposed.get(name, {})
            proposed[name] = {
                "value": value,
                "quote": quote,
                "source": "user",
                "turn": self.data["turn"],
                "revision": previous.get("revision", 0)
                + (previous.get("value") != value),
            }
        selected = (
            next((s for s in self.contract.stages if s.name == stage), None)
            if stage
            else None
        )
        if stage and selected is None:
            return {"status": "rejected", "reason": "Undeclared stage"}
        if selected and any(
            proposed.get(name, {}).get("value") is None
            for name in selected.required_facts
        ):
            return {
                "status": "rejected",
                "reason": "Stage prerequisites are incomplete",
            }
        if selected and selected.required_actions:
            succeeded = {
                row.get("tool_uuid")
                for row in (actions or {}).values()
                if row.get("status") == "succeeded"
                and row.get("task_id") == self.data["task_id"]
                and row.get("tool_uuid") in self.contract.tool_policies
                and all(
                    row.get("fact_revisions", {}).get(name)
                    == proposed.get(name, {}).get("revision")
                    for name in self.contract.tool_policies[
                        row["tool_uuid"]
                    ].required_facts
                )
            }
            if new_task or set(selected.required_actions) - succeeded:
                return {
                    "status": "rejected",
                    "reason": "Required business actions have not succeeded",
                }
        if new_task:
            self.data.update(
                task_id=str(uuid.uuid4()),
                status="active",
                stage=None,
                last_task_turn=self.data["turn"],
            )
            self.data.pop("action_proposals", None)
        self.data["facts"] = proposed
        if selected:
            self.data.update(
                stage=stage,
                status=("completed" if selected.outcome == "success" else "failed")
                if selected.terminal
                else "active",
                last_task_turn=self.data["turn"],
            )
        self.data["revision"] += 1
        return {"status": "updated", "state": self.snapshot()}

    def snapshot(self):
        return copy.deepcopy(self.data)

    def prompt(self):
        return (
            "\n\nAGENT BUSINESS CONTRACT\n"
            + json.dumps(
                {
                    "goal_type": self.contract.goal_type,
                    "goal": self.contract.goal,
                    "success_criteria": self.contract.success_criteria,
                    "stop_criteria": self.contract.stop_criteria,
                    "reply_style": self.contract.reply_style,
                    "facts": [f.model_dump() for f in self.contract.facts],
                    "stages": [s.model_dump() for s in self.contract.stages],
                    "state": self.snapshot(),
                },
                ensure_ascii=False,
            )
            + "\nKeep known facts across questions. Record corrections with update_task_state, quoting the current user. Task stages are internal; do not read them aloud. Only start a new task for a new user request after completing the current one. Tool status unknown means the result must be verified, not failed or successful. When a tool requires confirmation, explain its exact proposed arguments before asking for the required phrase."
        )


def register_task_state(engine, agent):
    state = engine.state_for_agent(agent)
    if not state.contract.enabled:
        return []
    schema = FunctionSchema(
        name="update_task_state",
        description="Record user-supplied facts/corrections or advance a task after prerequisites. Supply an exact quote from the current user turn. This does not execute business actions.",
        properties={
            "quote": {"type": "string"},
            "facts": {
                "type": "object",
                "properties": {
                    f.name: {"type": [f.value_type, "null"]}
                    for f in state.contract.facts
                },
                "additionalProperties": False,
            },
            "stage": {"type": "string", "enum": [s.name for s in state.contract.stages]}
            if state.contract.stages
            else {"type": "string"},
            "new_task": {"type": "boolean"},
        },
        required=["quote"],
    )

    async def update(params):
        if engine.active_agent is not agent:
            await params.result_callback(
                {"status": "rejected", "reason": "Agent visit is no longer active"}
            )
            return
        result = state.update(
            **params.arguments,
            actions=engine._gathered_context.get("business_actions", {}),
        )
        engine.record_context({"task_state": state.snapshot()})
        if result["status"] == "updated":
            await engine.refresh_task_state_prompt()
        await params.result_callback(result)

    agent.llm.register_function(schema.name, agent.bind_tool(engine, update))
    return [schema]

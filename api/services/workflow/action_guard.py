"""Execute HTTP actions through durable claims and explicit business outcomes."""

import asyncio
import copy
import json
import string
from types import SimpleNamespace

import jsonschema

from api.db import db_client
from api.schemas.agent_behavior import ToolExecutionPolicy
from api.services.workflow.runtime_profile import digest
from api.services.workflow.tools.custom_tool import (
    _resolve_preset_parameters,
    execute_http_tool,
    tool_to_function_schema,
)


def at_path(value, path):
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None, False
        value = value[part]
    return value, True


def business_success(result, policy):
    value, found = at_path(result, policy.success_path)
    return (
        isinstance(result, dict)
        and result.get("status") == "success"
        and found
        and type(value) is type(policy.success_equals)
        and value == policy.success_equals
    )


def business_outcome(result, policy):
    if business_success(result, policy):
        return "succeeded"
    if (
        policy.failure_path
        and isinstance(result, dict)
        and result.get("status") == "success"
    ):
        value, found = at_path(result, policy.failure_path)
        if (
            found
            and type(value) is type(policy.failure_equals)
            and value == policy.failure_equals
        ):
            return "failed"
    return "unknown"


def bounded_result(value):
    if len(json.dumps(value, ensure_ascii=False, default=str).encode()) > 64000:
        return {
            "evidence_truncated": True,
            "status": value.get("status") if isinstance(value, dict) else None,
        }
    return value


def confirmation_text(tool, arguments, policy, phrases):
    """Render an exact, parameter-complete proposal without model paraphrasing."""
    if policy.confirmation_template:
        fields = {
            name
            for _, name, _, _ in string.Formatter().parse(policy.confirmation_template)
            if name
        }
        if not fields.issubset(set(arguments) | {"arguments"}):
            raise ValueError("Confirmation template references unknown arguments")
        if "arguments" not in fields and set(arguments) - fields:
            raise ValueError(
                "Confirmation template must include every proposed argument"
            )
        values = {key: str(value) for key, value in arguments.items()}
        values["arguments"] = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        return policy.confirmation_template.format_map(values)
    detail = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return f"请确认执行“{tool.name}”：{detail}。如确认，请回复“{phrases[0]}”。"


async def acknowledge_proposals(engine, text):
    """The adapter calls this with delivered assistant text, never model intent."""
    state = engine.task_state
    proposals = state.data.get("action_proposals", {})
    for operation_id, proposal in list(proposals.items()):
        if proposal["text"] not in text:
            continue
        if await db_client.mark_action_proposed(
            organization_id=await engine._get_organization_id(),
            operation_id=operation_id,
            arguments_hash=proposal["arguments_hash"],
            turn=state.data["turn"],
        ):
            proposals.pop(operation_id, None)
    engine.record_context({"task_state": state.snapshot()})


async def execute_guarded_http(
    *,
    engine,
    tool,
    arguments,
    organization_id,
    executor=None,
    schema_override=None,
    tool_call_id=None,
    state=None,
):
    executor = executor or execute_http_tool
    raw = (tool.definition or {}).get("execution_policy")
    state = state or getattr(engine, "task_state", None)
    if not raw and state is not None and state.contract.enabled is True:
        override = state.contract.tool_policies.get(tool.tool_uuid)
        raw = override.model_dump() if override else None
    policy = ToolExecutionPolicy.model_validate(raw) if raw else None
    execute_kwargs = dict(
        tool=tool,
        arguments=arguments,
        call_context_vars=engine._call_context_vars,
        gathered_context_vars=engine._gathered_context,
        organization_id=organization_id,
    )
    if policy is None:
        return await executor(**execute_kwargs)
    if state is None:
        return {"status": "blocked", "reason": "structured_task_required"}
    if policy.required_facts and (
        not state.contract.enabled or state.missing(policy.required_facts)
    ):
        return {
            "status": "blocked",
            "reason": "required_facts_missing",
            "facts": state.missing(policy.required_facts),
        }
    if policy.effect == "read":
        return await executor(**execute_kwargs)
    if getattr(engine, "_is_realtime", False) is True:
        return {
            "status": "blocked",
            "reason": "guarded_actions_require_text_or_cascade_runtime",
        }
    if not state.contract.enabled or state.data["status"] != "active":
        return {"status": "blocked", "reason": "active_structured_task_required"}
    if state.data.get("text_revision") != state.data.get("input_revision"):
        return {"status": "blocked", "reason": "user_turn_in_progress"}
    schema = schema_override or tool_to_function_schema(tool)["function"]["parameters"]
    try:
        jsonschema.validate(arguments, schema)
        preset = _resolve_preset_parameters(
            tool.definition.get("config", {}),
            engine._call_context_vars,
            engine._gathered_context,
        )
        proposal_text = confirmation_text(
            tool, arguments, policy, state.contract.confirmation_phrases
        )
        if policy.idempotency_header and any(
            key.lower() == policy.idempotency_header.lower()
            for key in tool.definition.get("config", {}).get("headers", {})
        ):
            raise ValueError("Idempotency header already configured")
    except (jsonschema.ValidationError, ValueError):
        return {"status": "blocked", "reason": "invalid_arguments"}
    if any(key not in arguments for key in policy.identity_arguments):
        return {"status": "blocked", "reason": "operation_identity_missing"}
    operation_key = digest(
        [
            state.data["task_id"],
            tool.tool_uuid,
            {key: arguments[key] for key in policy.identity_arguments},
        ]
    )
    arguments_hash = digest(
        [
            arguments,
            preset,
            tool.definition,
            policy.model_dump(mode="json"),
            {
                key: {
                    "value": state.data["facts"].get(key, {}).get("value"),
                    "revision": state.data["facts"].get(key, {}).get("revision"),
                }
                for key in policy.required_facts
            },
        ]
    )
    request_turn = state.data["turn"]
    input_revision = state.data.get("input_revision", request_turn)
    operation = await db_client.claim_action(
        organization_id=organization_id,
        run_id=engine._workflow_run_id,
        operation_key=operation_key,
        tool_uuid=tool.tool_uuid,
        arguments=arguments,
        arguments_hash=arguments_hash,
        turn=state.data["turn"],
        confirmed=state.confirmation(),
        confirmation_required=policy.confirmation_required,
        lease_seconds=policy.lease_seconds,
    )
    public = {
        "operation_id": operation["id"],
        "status": operation["state"],
        "tool_uuid": tool.tool_uuid,
        "tool_call_id": tool_call_id,
        "task_id": state.data["task_id"],
        "fact_revisions": {
            key: state.data["facts"].get(key, {}).get("revision")
            for key in policy.required_facts
        },
    }
    engine._gathered_context.setdefault("business_actions", {})[operation["id"]] = (
        public
    )
    if operation["execute"] and (
        state.data.get("input_revision", state.data["turn"]) != input_revision
        or engine.task_state is not state
    ):
        await db_client.release_unsubmitted_action(
            organization_id=organization_id,
            operation_id=operation["id"],
            owner_token=operation["owner_token"],
            turn=state.data["turn"],
        )
        public["status"] = "awaiting_confirmation"
        return {
            **public,
            "reason": "conversation_changed_before_dispatch",
            "instruction": "Recheck the latest request and propose the current arguments again.",
        }
    if not operation["execute"]:
        if operation["state"] == "awaiting_confirmation":
            proposals = state.data.setdefault("action_proposals", {})
            proposals[operation["id"]] = {
                "text": proposal_text,
                "arguments_hash": arguments_hash,
            }
            # A bounded checkpoint; an evicted proposal simply needs to be
            # presented again before it can be confirmed.
            while len(proposals) > 20:
                proposals.pop(next(iter(proposals)))
            return {
                **public,
                "arguments": arguments,
                "proposal": proposal_text,
                "instruction": "Output the proposal verbatim and wait for a new user turn. Do not claim completion.",
                "confirmation_phrases": state.contract.confirmation_phrases,
            }
        if operation["state"] == "unknown" and policy.reconcile_tool_uuid:
            evaluation = getattr(engine, "evaluation_policy", None)
            if evaluation is not None:
                frozen = evaluation.tool_definitions.get(policy.reconcile_tool_uuid)
                lookup = (
                    SimpleNamespace(
                        tool_uuid=policy.reconcile_tool_uuid,
                        name=policy.reconcile_tool_uuid,
                        description="Frozen reconciliation tool",
                        definition=frozen,
                    )
                    if frozen
                    else None
                )
            else:
                lookup = await db_client.get_tool_by_uuid(
                    policy.reconcile_tool_uuid, organization_id
                )
            lookup_raw = (
                (lookup.definition or {}).get("execution_policy") if lookup else None
            )
            if (
                not lookup_raw
                and policy.reconcile_tool_uuid in state.contract.tool_policies
            ):
                lookup_raw = state.contract.tool_policies[
                    policy.reconcile_tool_uuid
                ].model_dump(mode="json")
            lookup_policy = (
                ToolExecutionPolicy.model_validate(lookup_raw) if lookup_raw else None
            )
            if (
                lookup is None
                or lookup_policy is None
                or lookup_policy.effect != "read"
                or (lookup.definition or {}).get("type") != "http_api"
            ):
                return {**public, "reason": "reconciliation_requires_read_tool"}
            lookup_args = {}
            for key, source in policy.reconcile_arguments.items():
                value, found = at_path(
                    {
                        "arguments": operation["arguments"],
                        "operation_id": operation["id"],
                    },
                    source,
                )
                if not found:
                    return {**public, "reason": "reconciliation_argument_missing"}
                lookup_args[key] = value
            try:
                jsonschema.validate(
                    lookup_args,
                    tool_to_function_schema(lookup)["function"]["parameters"],
                )
                result = await executor(
                    tool=lookup,
                    arguments=lookup_args,
                    organization_id=organization_id,
                    call_context_vars=engine._call_context_vars,
                    gathered_context_vars=engine._gathered_context,
                )
            except Exception:
                return {**public, "reason": "reconciliation_unavailable"}
            outcome = business_outcome(result, policy)
            if outcome != "unknown":
                saved = await db_client.finish_action(
                    organization_id=organization_id,
                    operation_id=operation["id"],
                    owner_token=operation["owner_token"],
                    state=outcome,
                    result=bounded_result(result),
                )
                public["status"] = outcome if saved else "unknown"
                return {**public, "result": result}
        return {
            **public,
            "result": operation["result"],
            "instruction": "Never repeat an executing or unknown operation. Verify its outcome first.",
        }
    request_tool = SimpleNamespace(
        name=tool.name,
        tool_uuid=tool.tool_uuid,
        definition=copy.deepcopy(tool.definition),
    )
    if policy.idempotency_header:
        headers = request_tool.definition.setdefault("config", {}).setdefault(
            "headers", {}
        )
        headers[policy.idempotency_header] = operation["id"]
    try:
        result = await executor(
            **{**execute_kwargs, "tool": request_tool, "preset_params": preset}
        )
        outcome = business_outcome(result, policy)
        saved = await db_client.finish_action(
            organization_id=organization_id,
            operation_id=operation["id"],
            owner_token=operation["owner_token"],
            state=outcome,
            result=bounded_result(result),
        )
        public["status"] = outcome if saved else "unknown"
        return {**public, "result": result}
    except BaseException as error:
        public["status"] = "unknown"
        await asyncio.shield(
            db_client.finish_action(
                organization_id=organization_id,
                operation_id=operation["id"],
                owner_token=operation["owner_token"],
                state="unknown",
                result={"reason": "execution_interrupted"},
            )
        )
        if isinstance(error, asyncio.CancelledError):
            raise
        if not isinstance(error, Exception):
            raise
        return {
            **public,
            "reason": "execution_outcome_unknown",
            "instruction": "Verify the business outcome before reporting success or failure; do not repeat the operation.",
        }

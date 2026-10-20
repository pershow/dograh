"""Fixed-script simulation against Dograh's real Pipecat text runtime."""

import asyncio
import uuid

from api.db import db_client
from api.schemas.agent_workbench import SimulationSuite
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.evaluation_policy import EvaluationPolicy


def resolve_node_references(case, graph):
    """Accept stable IDs or unique exact names, never guess an ambiguous node."""
    nodes = graph.get("nodes", [])
    ids = {str(node["id"]) for node in nodes}
    references = {}
    for assertion in case.assertions:
        if assertion.kind != "node_visited":
            continue
        value = assertion.value
        if value in ids:
            references[value] = value
            continue
        matches = [
            str(node["id"])
            for node in nodes
            if node.get("data", {}).get("name") == value
        ]
        if len(matches) != 1:
            raise WorkbenchError(
                f"node_visited {value!r} must identify an existing node ID or unique node name"
            )
        references[value] = matches[0]
    return references


def tuning_evidence(evaluation):
    """Omit duplicate speech events and billing usage from each repeated model input."""
    results = []
    for case in evaluation["results"]:
        messages = case.get("transcript", [])
        # Distribute a bounded transcript allowance across all cases and turns.
        limit = max(
            80, 24000 // max(1, len(evaluation["results"])) // max(1, len(messages))
        )
        results.append(
            {
                "name": case["name"],
                "status": case["status"],
                "error": case.get("error"),
                "assertions": case.get("assertions", []),
                "transcript": [
                    {
                        **m,
                        "text": m["text"][:limit]
                        + ("… [truncated]" if len(m["text"]) > limit else ""),
                    }
                    for m in messages
                ],
                "visited_nodes": [
                    {
                        k: event.get("payload", event.get("data", {})).get(k)
                        for k in ("node_id", "node_name", "to_node_id")
                    }
                    for event in case.get("events", [])
                    if event.get("type") == "node_transition"
                ],
                "tool_calls": [
                    {k: call.get(k) for k in ("name", "arguments")}
                    for call in case.get("tool_calls", [])
                ],
            }
        )
    return results


def check_assertions(
    case, transcript, events, context, completed, calls, node_references=None
):
    assistant = "\n".join(m["text"] for m in transcript if m["role"] == "assistant")
    nodes = {
        str(e.get("payload", e.get("data", {})).get("node_id", ""))
        for e in events
        if e.get("type") == "node_transition"
    }
    # The runner's transition payload also carries to_node_id in some versions.
    nodes |= {
        str(e.get("payload", e.get("data", {})).get("to_node_id", ""))
        for e in events
        if e.get("type") == "node_transition"
    }
    results = []
    for assertion in case.assertions:
        kind, value = assertion.kind, assertion.value
        if kind == "contains":
            passed = str(value).casefold() in assistant.casefold()
        elif kind == "not_contains":
            passed = str(value).casefold() not in assistant.casefold()
        elif kind == "node_visited":
            passed = str((node_references or {}).get(value, value)) in nodes
        elif kind == "tool_called":
            passed = any(c["name"] == value for c in calls)
        elif kind == "completed":
            passed = completed == bool(value if value is not None else True)
        else:
            current = context
            for key in (assertion.key or "").split("."):
                current = current.get(key) if isinstance(current, dict) else None
            passed = current == value
        results.append({**assertion.model_dump(), "passed": passed})
    return results


async def evaluate(org, session_id, job, candidate_id, suite_id, model_configuration):
    from api.services.workflow.text_chat_runner import execute_text_chat_pending_turn

    candidate = await db_client.wb_get(org, session_id, "candidates", candidate_id)
    suite_row = await db_client.wb_get(org, session_id, "suites", suite_id)
    suite = SimulationSuite.model_validate(suite_row["payload"])
    context = await db_client.wb_context(org, session_id)
    evaluation = await db_client.wb_evaluation(
        org, session_id, job["id"], job["lease_token"], candidate_id, suite_id
    )
    results = []
    for case in suite.cases:
        policy = EvaluationPolicy(case, model_configuration)
        run_id, transcript, events, turns, checkpoint = None, [], [], [], None
        completed, gathered, usage = False, {}, []
        try:
            node_references = resolve_node_references(
                case, candidate["snapshot"]["workflow_json"]
            )
            policy.validate(candidate["snapshot"]["workflow_json"])
            run_id = await db_client.wb_create_run(
                org,
                session_id,
                job["id"],
                job["lease_token"],
                candidate_id,
                {
                    **(candidate["snapshot"].get("template_context_variables") or {}),
                    **case.initial_context,
                },
            )
            for message in [None, *case.user_messages]:
                await db_client.wb_heartbeat(
                    org, session_id, job["id"], job["lease_token"]
                )
                if completed:
                    break
                turn = {
                    "id": str(uuid.uuid4()),
                    "status": "pending",
                    "user_message": {"text": message} if message is not None else None,
                }
                turns.append(turn)
                if message is not None:
                    transcript.append({"role": "user", "text": message})
                result = await asyncio.wait_for(
                    execute_text_chat_pending_turn(
                        workflow_id=context["session"]["workflow_id"],
                        workflow_run_id=run_id,
                        session_data={"turns": turns},
                        checkpoint=checkpoint,
                        evaluation_policy=policy,
                    ),
                    75,
                )
                checkpoint, gathered, completed = (
                    result.checkpoint,
                    result.gathered_context,
                    result.is_completed,
                )
                turn["status"] = "completed"
                turn["assistant_message"] = {"text": result.assistant_text or ""}
                transcript.append(
                    {"role": "assistant", "text": result.assistant_text or ""}
                )
                events.extend(result.events)
                usage.append(result.usage)
                if message is not None and not result.assistant_text and not completed:
                    policy.errors.append(
                        "Text runtime returned no response to a simulated user turn"
                    )
                if policy.errors:
                    raise WorkbenchError("; ".join(policy.errors))
            assertions = check_assertions(
                case,
                transcript,
                events,
                gathered,
                completed,
                policy.calls,
                node_references,
            )
            status = "passed" if all(a["passed"] for a in assertions) else "failed"
            if not any(m["text"] for m in transcript if m["role"] == "assistant"):
                status = "inconclusive"
            outcome = {"name": case.name, "status": status, "assertions": assertions}
        except (TimeoutError, WorkbenchError, ValueError) as error:
            outcome = {
                "name": case.name,
                "status": "error",
                "error": str(error)[:2000],
                "assertions": [],
            }
        finally:
            if run_id is not None:
                # Evaluation runs never enter ordinary completion integrations/billing.
                await db_client.update_workflow_run(
                    run_id, is_completed=True, gathered_context=gathered
                )
        outcome.update(
            {
                "run_id": run_id,
                "transcript": transcript,
                "events": events,
                "tool_calls": policy.calls,
                "usage": usage,
            }
        )
        results.append(outcome)
        summary = {
            status: sum(r["status"] == status for r in results)
            for status in ("passed", "failed", "error", "inconclusive")
        }
        summary.update(
            {
                "total": len(suite.cases),
                "candidate_fingerprint": candidate["fingerprint"],
                "suite_fingerprint": suite_row["fingerprint"],
                "model": model_configuration.llm.model,
                "model_configuration_fingerprint": job.get("output", {}).get(
                    "model_configuration_fingerprint"
                ),
            }
        )
        await db_client.wb_evaluation_result(
            org,
            session_id,
            job["id"],
            job["lease_token"],
            evaluation["id"],
            results,
            "completed" if len(results) == len(suite.cases) else "running",
            summary,
        )
    return {
        "id": evaluation["id"],
        "results": results,
        "summary": summary,
        "candidate_id": candidate_id,
        "suite_id": suite_id,
    }


def acceptable_improvement(baseline, proposed):
    old, new = baseline["results"], proposed["results"]
    if len(old) != len(new) or any(
        item["status"] in ("error", "inconclusive") for item in new
    ):
        return False
    if any(
        a["status"] == "passed" and b["status"] != "passed" for a, b in zip(old, new)
    ):
        return False
    return sum(r["status"] == "passed" for r in new) > sum(
        r["status"] == "passed" for r in old
    )

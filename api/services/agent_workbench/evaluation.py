"""Read-only simulations against Dograh's real Pipecat text runtime."""

import asyncio
import copy
import time
import uuid

from api.db import db_client
from api.schemas.agent_workbench import SimulationSuite
from api.services.agent_workbench.errors import LeaseLost, WorkbenchError
from api.services.agent_workbench.evaluation_policy import EvaluationPolicy


def resolve_node_references(case, graph):
    """Accept stable IDs or unique exact names, never guess an ambiguous node."""
    nodes = graph.get("nodes", [])
    ids = {str(node["id"]) for node in nodes}
    references = {}
    for assertion in case.assertions:
        if assertion.kind not in {"node_visited", "node_not_visited", "node_sequence"}:
            continue
        for value in (
            assertion.value if assertion.kind == "node_sequence" else [assertion.value]
        ):
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
                    f"{assertion.kind} {value!r} must identify an existing node ID or unique node name"
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
                "reference_review": case.get("reference_review"),
                "validity": case.get("validity"),
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
    replies = [m for m in transcript if m["role"] == "assistant"]
    assistant = "\n".join(m["text"] for m in replies)
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
        text = assistant
        if assertion.turn is not None:
            matched = [
                m for i, m in enumerate(replies) if m.get("turn", i) == assertion.turn
            ]
            if not matched:
                results.append(
                    {
                        **assertion.model_dump(),
                        "passed": False,
                        "reason": "Requested assistant turn did not occur",
                    }
                )
                continue
            text = "\n".join(m["text"] for m in matched)
        if kind == "contains":
            passed = str(value).casefold() in text.casefold()
        elif kind == "contains_any":
            passed = any(v.casefold() in text.casefold() for v in value)
        elif kind == "not_contains":
            passed = str(value).casefold() not in text.casefold()
        elif kind in {"node_visited", "node_not_visited"}:
            passed = str((node_references or {}).get(value, value)) in nodes
            if kind == "node_not_visited":
                passed = not passed
        elif kind == "node_sequence":
            actual = [
                str(
                    e.get("payload", e.get("data", {})).get("to_node_id")
                    or e.get("payload", e.get("data", {})).get("node_id", "")
                )
                for e in events
                if e.get("type") == "node_transition"
            ]
            cursor = iter(actual)
            passed = all(
                any(node == str((node_references or {}).get(v, v)) for node in cursor)
                for v in value
            )
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


def apply_reference_review(outcome, review):
    """Replace review assertions without changing runtime evidence or explicit checks."""
    assertions = [a for a in outcome.get("assertions", []) if a["kind"] != "reference"]
    explicit_failure = any(
        not a["passed"] and not a.get("informational") for a in assertions
    )
    assertions.extend(
        {
            "kind": "reference",
            "value": c["criterion"],
            "passed": c["status"] == "passed",
            "turn": c["turn"],
            "reason": c["reason"],
            "reference_quote": c["reference_quote"],
            "actual_quote": c["actual_quote"],
            "category": c.get("category"),
            "severity": c.get("severity"),
            "layer": c.get("layer"),
            "repair_target": c.get("repair_target"),
            "citations": c.get("citations", []),
            "informational": c["status"] == "inconclusive",
        }
        for c in review.get("checks", [])
    )
    if not review.get("checks"):
        assertions.append(
            {
                "kind": "reference",
                "value": "参考对话评审未完成",
                "passed": False,
                "reason": review.get("reason", "缺少评审结果"),
            }
        )
    return {
        **outcome,
        "assertions": assertions,
        "reference_review": review,
        "status": "inconclusive"
        if outcome.get("validity", {}).get("status") in {"invalid", "inconclusive"}
        else "failed"
        if explicit_failure
        else review["status"],
    }


async def evaluate(
    org,
    session_id,
    job,
    candidate_id,
    suite_id,
    model_configuration,
    *,
    reference_judge=None,
    on_progress=None,
    prior_results=None,
    runtime_receipt=None,
    driver_llm=None,
    driver_protocol=None,
    case_semaphore=None,
):
    from api.services.workflow.text_chat_runner import execute_text_chat_pending_turn

    candidate = await db_client.wb_get(org, session_id, "candidates", candidate_id)
    suite_row = await db_client.wb_get(org, session_id, "suites", suite_id)
    suite = SimulationSuite.model_validate(suite_row["payload"])
    context = await db_client.wb_context(org, session_id)
    evaluation = await db_client.wb_evaluation(
        org, session_id, job["id"], job["lease_token"], candidate_id, suite_id
    )
    if reference_judge is None and any(c.reference_dialogue for c in suite.cases):
        from api.services.agent_workbench.basis import ensure_basis
        from api.services.agent_workbench.dependencies import prepare_models
        from api.services.agent_workbench.test_tasks import judge_reference

        reviewer = await prepare_models(org, candidate["snapshot"], "build")
        driver_llm = driver_llm or reviewer.effective.llm
        basis = await ensure_basis(
            org,
            session_id,
            job,
            candidate["snapshot"],
            reference_asset_ids={c.source.get("asset_id") for c in suite.cases},
            llm=reviewer.effective.llm,
            protocol=context["session"]["model_protocol"],
        )

        async def quiet_event(kind, payload):
            if kind == "model.error":
                await db_client.wb_emit(
                    org, session_id, kind,
                    {**payload, "job_id": job["id"], "stage": "reference_review"},
                    job_id=job["id"], token=job["lease_token"],
                )

        async def reference_judge(case, transcript, events):
            return await judge_reference(
                case,
                transcript,
                events,
                candidate,
                reviewer.effective.llm,
                quiet_event,
                context["session"]["model_protocol"],
                instructions=job["input"].get("text")
                or job["input"].get("instruction")
                or "按冻结用例评估当前选择的版本。",
                verifier_llm=reviewer.effective.llm,
                basis=basis,
            )

    needs_driver = any(c.mode != "scripted" or c.reference_turns for c in suite.cases)
    if needs_driver and driver_llm is None:
        from api.services.agent_workbench.dependencies import prepare_models

        driver_llm = (
            await prepare_models(org, candidate["snapshot"], "build")
        ).effective.llm

    from api.services.agent_workbench.config import WorkbenchSettings

    started = time.monotonic()
    results = {
        i: {
            **prior_results[c.name],
            "runtime_responded": bool(prior_results[c.name].get("runtime_responded"))
            or any(
                m.get("role") == "assistant" and m.get("text")
                for m in prior_results[c.name].get("transcript", [])
            ),
            "reused_runtime": True,
        }
        for i, c in enumerate(suite.cases)
        if prior_results
        and c.name in prior_results
        and not prior_results[c.name].get("review_pending")
        and not prior_results[c.name].get("replay_pending")
    }
    semaphore = case_semaphore or asyncio.Semaphore(
        WorkbenchSettings.load().evaluation_concurrency
    )
    persist_lock = asyncio.Lock()
    partial_results = {}

    def stored_results():
        combined = {**partial_results, **results}
        return [combined[i] for i in sorted(combined)]

    async def finish_review(case):
        # Copy before adding the new review so historical evaluation rows and
        # callers' cached evidence remain immutable.
        outcome = copy.deepcopy(prior_results[case.name])
        outcome.update(runtime_responded=True, reused_runtime=True)
        outcome.pop("review_pending", None)
        await db_client.wb_emit(
            org,
            session_id,
            "evaluation.case.progress",
            {
                "job_id": job["id"],
                "evaluation_id": evaluation["id"],
                "run_id": outcome["run_id"],
                "name": case.name,
                "phase": "reviewing",
                "total_turns": len(case.user_messages),
                "turn": sum(m["role"] == "user" for m in outcome["transcript"]),
                "reused_runtime": True,
            },
            job_id=job["id"],
            token=job["lease_token"],
        )
        old_review = outcome.get("reference_review")
        if old_review:
            outcome.setdefault("previous_reference_reviews", []).append(
                {
                    "evaluation_id": outcome.get("original_evaluation_id"),
                    "review": old_review,
                }
            )
        try:
            if reference_judge is None:
                raise WorkbenchError("缺少参考对话评审器。")
            review = await reference_judge(
                case, outcome["transcript"], outcome.get("events", [])
            )
        except LeaseLost:
            raise
        except WorkbenchError as error:
            review = {"status": "inconclusive", "reason": str(error)}
        result = apply_reference_review(outcome, review)
        await db_client.wb_emit(
            org,
            session_id,
            "evaluation.case.progress",
            {
                "job_id": job["id"],
                "evaluation_id": evaluation["id"],
                "run_id": outcome["run_id"],
                "name": case.name,
                "phase": "completed",
                "status": result["status"],
                "total_turns": len(case.user_messages),
                "reused_runtime": True,
            },
            job_id=job["id"],
            token=job["lease_token"],
        )
        return result

    async def run_case(index, case):
        policy = EvaluationPolicy(case, model_configuration)
        run_id, transcript, events, turns, checkpoint = None, [], [], [], None
        completed, gathered, usage = False, {}, []
        driver = None

        async def publish_partial():
            async with persist_lock:
                partial_results[index] = copy.deepcopy(
                    {
                        "name": case.name,
                        "status": "running",
                        "mode": case.mode,
                        "run_id": run_id,
                        "trace_url": gathered.get("trace_url"),
                        "source": case.source,
                        "transcript": transcript,
                        "events": events,
                        "assertions": [],
                        "tool_calls": policy.calls,
                        "history": [t.model_dump() for t in case.history],
                        "validity": driver.receipt(completed)
                        if driver
                        else {"status": "unchecked"},
                    }
                )
                await db_client.wb_evaluation_result(
                    org,
                    session_id,
                    job["id"],
                    job["lease_token"],
                    evaluation["id"],
                    stored_results(),
                    "running",
                    summary(),
                )

        async def case_event(phase, **details):
            await db_client.wb_emit(
                org,
                session_id,
                "evaluation.case.progress",
                {
                    "job_id": job["id"],
                    "evaluation_id": evaluation["id"],
                    "run_id": run_id,
                    "name": case.name,
                    "phase": phase,
                    "total_turns": len(case.user_messages),
                    "mode": case.mode,
                    **details,
                },
                job_id=job["id"],
                token=job["lease_token"],
            )

        try:
            node_references = resolve_node_references(
                case, candidate["snapshot"]["workflow_json"]
            )
            policy.validate(candidate["snapshot"]["workflow_json"])
            if case.mode != "scripted" or case.reference_turns:
                from api.services.agent_workbench.simulation import ConversationDriver

                async def driver_event(kind, payload):
                    if kind == "model.progress":
                        await case_event(
                            "preparing" if run_id is None else "customer",
                            detail=payload.get("phase"),
                        )

                driver = ConversationDriver(
                    case,
                    candidate,
                    driver_llm,
                    driver_event,
                    driver_protocol or context["session"].get("model_protocol", "chat"),
                    customer_llm=model_configuration.llm
                    if getattr(model_configuration.llm, "provider", "")
                    in {"openai", "openrouter", "atlascloud", "hopper"}
                    else None,
                )
                await case_event("preparing")
                frozen = (
                    (prior_results or {})
                    .get(case.name, {})
                    .get("validity", {})
                    .get("scenario")
                )
                if case.mode == "dynamic" and frozen:
                    driver.scenario = copy.deepcopy(frozen)
                else:
                    await driver.prepare()
                await case_event("scenario_ready", validity=driver.receipt())
                await publish_partial()
                if driver.stop_reason:
                    raise WorkbenchError(
                        "真实上下文无法唯一对应运行节点："
                        + driver.context_selection["reason"]
                    )
                if case.mode == "single_step":
                    checkpoint = driver.checkpoint()
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
            turn_index = 0
            while True:
                await db_client.wb_heartbeat(
                    org, session_id, job["id"], job["lease_token"]
                )
                if completed:
                    break
                if case.mode == "single_step":
                    if turn_index > 0:
                        break
                    message = case.user_messages[0]
                    turn_index = 1
                elif turn_index == 0:
                    message = None
                elif driver:
                    await case_event("checking_alignment", turn=turn_index)
                    message = await driver.next(transcript, turn_index - 1)
                    await case_event(
                        "alignment_checked",
                        turn=turn_index,
                        check=driver.checks[-1] if driver.checks else None,
                    )
                    if message is None:
                        break
                else:
                    if turn_index > len(case.user_messages):
                        break
                    message = case.user_messages[turn_index - 1]
                turn = {
                    "id": str(uuid.uuid4()),
                    "status": "pending",
                    "user_message": {"text": message} if message is not None else None,
                }
                turns.append(turn)
                if message is not None:
                    transcript.append(
                        {"role": "user", "text": message, "turn": turn_index}
                    )
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
                    {
                        "role": "assistant",
                        "text": result.assistant_text or "",
                        "turn": turn_index,
                    }
                )
                events.extend({**event, "turn": turn_index} for event in result.events)
                usage.append(result.usage)
                await publish_partial()
                await case_event("replaying", turn=turn_index)
                if message is not None and not result.assistant_text and not completed:
                    policy.errors.append(
                        "Text runtime returned no response to a simulated user turn"
                    )
                if policy.errors:
                    raise WorkbenchError("; ".join(policy.errors))
                turn_index += 1
            assertions = check_assertions(
                case,
                transcript,
                events,
                gathered,
                completed,
                policy.calls,
                node_references,
            )
            consumed = sum(m["role"] == "user" for m in transcript)
            explicit_assertions_passed = all(a["passed"] for a in assertions)
            if case.mode == "scripted":
                assertions.append(
                    {
                        "kind": "script_consumed",
                        "value": len(case.user_messages),
                        "actual": consumed,
                        "passed": consumed == len(case.user_messages),
                        "reason": "固定脚本执行覆盖率；提前结束本身不证明 Agent 违反流程，需结合实际分支复核。",
                        "informational": bool(case.reference_dialogue),
                    }
                )
            status = (
                "passed"
                if (
                    explicit_assertions_passed
                    if case.reference_dialogue
                    else all(a["passed"] for a in assertions)
                )
                else "failed"
            )
            if not any(m["text"] for m in transcript if m["role"] == "assistant"):
                status = "inconclusive"
            review = None
            if driver:
                events.append(
                    {"type": "test_validity", "payload": driver.receipt(completed)}
                )
            if case.reference_dialogue:
                if reference_judge is None:
                    status = "inconclusive"
                    assertions.append(
                        {
                            "kind": "reference",
                            "value": "缺少参考对话评审",
                            "passed": False,
                        }
                    )
                else:
                    try:
                        await case_event("reviewing", turn=consumed)
                        review = await reference_judge(case, transcript, events)
                        reviewed = apply_reference_review(
                            {"assertions": assertions}, review
                        )
                        assertions, status = reviewed["assertions"], reviewed["status"]
                    except LeaseLost:
                        raise
                    except WorkbenchError as error:
                        if status != "failed":
                            status = "inconclusive"
                        review = {"status": "inconclusive", "reason": str(error)}
                        assertions.append(
                            {
                                "kind": "reference",
                                "value": "参考对话评审未完成",
                                "passed": False,
                                "reason": str(error),
                            }
                        )
            outcome = {
                "name": case.name,
                "status": status,
                "assertions": assertions,
                "reference_review": review,
            }
            if driver and driver.receipt(completed)["status"] != "valid":
                outcome["observed_review_status"] = outcome["status"]
                outcome["status"] = "inconclusive"
        except LeaseLost:
            raise
        except (TimeoutError, WorkbenchError, ValueError) as error:
            if driver and not driver.stop_reason:
                driver.stop_reason = "execution_error"
            outcome = {
                "name": case.name,
                "status": "error",
                "error": (str(error) or "单轮仿真超过 75 秒未完成")[:2000],
                "assertions": [],
            }
            if driver and driver.stop_reason == "context_ambiguous":
                outcome["status"] = "inconclusive"
        finally:
            if run_id is not None:
                # Evaluation runs never enter ordinary completion integrations/billing.
                await db_client.update_workflow_run(
                    run_id, is_completed=True, gathered_context=gathered
                )
        outcome.update(
            {
                "run_id": run_id,
                "trace_url": gathered.get("trace_url"),
                "source": case.source,
                "transcript": transcript,
                "events": events,
                "tool_calls": policy.calls,
                "usage": usage,
                "mode": case.mode,
                "history": [t.model_dump() for t in case.history],
                "validity": driver.receipt(completed)
                if driver
                else {"mode": case.mode, "status": "unchecked"},
                "runtime_responded": any(
                    m["role"] == "assistant" and m["text"] for m in transcript
                ),
            }
        )
        await case_event(
            "completed",
            status=outcome["status"],
            turn=sum(m["role"] == "user" for m in transcript),
        )
        return outcome

    def summary():
        from api.services.agent_workbench.review import review_counts

        values = list(results.values())
        result = {
            status: sum(r["status"] == status for r in values)
            for status in ("passed", "failed", "error", "inconclusive")
        }
        result.update(
            {
                **review_counts(values),
                "valid": sum(
                    r.get("validity", {}).get("status") == "valid" for r in values
                ),
                "invalid": sum(
                    r.get("validity", {}).get("status") == "invalid" for r in values
                ),
                "validity_inconclusive": sum(
                    r.get("validity", {}).get("status") == "inconclusive"
                    for r in values
                ),
                "total": len(suite.cases),
                "attempted": sum(bool(r.get("run_id")) for r in values),
                "executed": sum(bool(r.get("runtime_responded")) for r in values),
                "reused": sum(bool(r.get("reused_runtime")) for r in values),
                "candidate_fingerprint": candidate["fingerprint"],
                "suite_fingerprint": suite_row["fingerprint"],
                "model": model_configuration.llm.model,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "model_configuration_fingerprint": (runtime_receipt or {}).get(
                    "model_configuration_fingerprint"
                )
                or job.get("output", {}).get(
                    "evaluation_model_configuration_fingerprint"
                )
                or job.get("output", {}).get("model_configuration_fingerprint"),
            }
        )
        if runtime_receipt:
            result.update(
                source_evaluation_id=runtime_receipt["id"],
                model_configuration_fingerprint=runtime_receipt.get(
                    "model_configuration_fingerprint"
                ),
                reviewer_model_configuration_fingerprint=job.get("output", {}).get(
                    "model_configuration_fingerprint"
                ),
                runtime_replayed=False,
            )
        return result

    async def run_and_persist(index, case):
        async with semaphore:
            outcome = (
                await finish_review(case)
                if prior_results
                and prior_results.get(case.name, {}).get("review_pending")
                else await run_case(index, case)
            )
        async with persist_lock:
            results[index] = outcome
            await db_client.wb_evaluation_result(
                org,
                session_id,
                job["id"],
                job["lease_token"],
                evaluation["id"],
                stored_results(),
                "completed" if len(results) == len(suite.cases) else "running",
                summary(),
            )
            if on_progress:
                await on_progress(
                    evaluation["id"],
                    summary(),
                    sum(bool(r.get("runtime_responded")) for r in results.values()),
                )

    async with asyncio.TaskGroup() as group:
        if results:
            await db_client.wb_evaluation_result(
                org,
                session_id,
                job["id"],
                job["lease_token"],
                evaluation["id"],
                [results[i] for i in sorted(results)],
                "completed" if len(results) == len(suite.cases) else "running",
                summary(),
            )
        # Register the evaluation before its first model call, so an interrupted
        # first case can be found and resumed from its frozen scenario.
        if on_progress:
            await on_progress(
                evaluation["id"],
                summary(),
                sum(bool(r.get("runtime_responded")) for r in results.values()),
            )
        for index, case in enumerate(suite.cases):
            if index not in results:
                group.create_task(run_and_persist(index, case))
    return {
        "id": evaluation["id"],
        "results": [results[i] for i in sorted(results)],
        "summary": summary(),
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
    improved = False
    for a, b in zip(old, new):
        if a.get("name") != b.get("name"):
            return False
        prior, later = a.get("assertions", []), b.get("assertions", [])
        if len(prior) != len(later):
            return False
        for left, right in zip(prior, later):
            if any(
                left.get(k) != right.get(k) for k in ("kind", "value", "key", "turn")
            ):
                return False
            if left.get("passed") and not right.get("passed"):
                return False
            improved |= bool(right.get("passed") and not left.get("passed"))
    return improved or sum(r["status"] == "passed" for r in new) > sum(
        r["status"] == "passed" for r in old
    )

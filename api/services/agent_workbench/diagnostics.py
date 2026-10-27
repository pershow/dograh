"""Read-only causal diagnosis from frozen runs and isolated model completions.

No tool returned by a model is executed. Temporary interventions never become an
Agent definition, candidate, test expectation, or memory entry.
"""

import asyncio
import copy
import json
import uuid

from api.db import db_client
from api.services.agent_workbench.context import redact
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.configuration.masking import mask_workflow_definition

SECTIONS = ("overview", "transcript", "execution", "evaluation", "request", "definition", "probes")
EVALUATION_SECTIONS = ("all", "transcript", "execution", "review", "source")


def execution_summary(case):
    events = case.get("events") or []
    transitions = [
        {"turn": e.get("turn"), **(e.get("payload") or e.get("data") or {})}
        for e in events
        if e.get("type") == "node_transition"
    ]
    return {
        "transitions": transitions,
        "recorded_transition_count": len(transitions),
        "request_count": sum(e.get("type") == "llm_request" for e in events),
        "recorded_nodes_visited": (case.get("gathered_context") or {}).get(
            "nodes_visited"
        ),
        "tool_events": [
            e
            for e in events
            if e.get("type")
            in {"function_call", "function_call_result", "tool_call", "tool_result"}
        ],
        "note": "Transitions are recorded execution evidence. Empty fixture tool_calls alone does not prove no routing calls. Missing events are not proof of no transition.",
    }


def page(value, offset=0, limit=16000):
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 24000
    ):
        raise WorkbenchError("Invalid evidence page bounds")
    text = json.dumps(redact(value), ensure_ascii=False)
    end = min(offset + limit, len(text))
    return {
        "text": text[offset:end],
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "characters": len(text),
    }


async def reconstruct_request(packet, turn, request_index=0):
    """Prefer captured request; never invent missing historical tool messages."""
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter
    from pipecat.processors.aggregators.llm_context import LLMContext

    from api.services.workflow.dto import ReactFlowDTO
    from api.services.workflow.pipecat_engine_context_composer import (
        compose_functions_for_node,
        compose_system_prompt_for_node,
    )
    from api.services.workflow.workflow_graph import WorkflowGraph
    from api.utils.template_renderer import render_template

    case = packet.get("case") or {}
    definition = packet.get("definition")
    events = case.get("events") or []
    captured = [
        e["payload"]
        for e in events
        if e.get("type") == "llm_request" and e.get("turn") == turn
    ]
    if captured:
        if not 0 <= request_index < len(captured):
            raise WorkbenchError("Request index does not exist in this turn")
        data = captured[request_index]
        return {
            **copy.deepcopy(data),
            "provenance": "captured",
            "limitations": data.get("limitations", []),
            "replayable": bool(data.get("request")) and not data.get("redacted", True),
        }
    if not definition:
        raise WorkbenchError(
            "This run has no frozen definition; current draft is not historical evidence"
        )
    if request_index != 0:
        raise WorkbenchError(
            "Historical request indices are unavailable without request capture"
        )
    transitions = [
        e
        for e in events
        if e.get("type") == "node_transition"
        and isinstance(e.get("turn"), int)
        and (e["turn"] < turn or turn == 0 and e["turn"] == 0)
    ]
    graph = WorkflowGraph(
        ReactFlowDTO.model_validate(definition["workflow_json"]),
        skip_instance_constraints_for={"trigger"},
    )
    if not transitions:
        raise WorkbenchError(
            "No recorded entry node before this turn; cannot assume the current draft/start node"
        )
    payload = transitions[-1].get("payload") or transitions[-1].get("data") or {}
    node_id = str(payload.get("to_node_id") or payload.get("node_id") or "")
    if node_id not in graph.nodes:
        raise WorkbenchError("Recorded node is absent from the frozen definition")
    messages = [
        {"role": m["role"], "content": m.get("text", "")}
        for m in case.get("history", [])
    ]
    target = [
        m
        for m in case.get("transcript", [])
        if m.get("turn") == turn and m.get("role") == "user"
    ]
    if len(target) != 1:
        raise WorkbenchError(
            "Choose a recorded user turn with exactly one user message"
        )
    for m in case.get("transcript", []):
        if not isinstance(m.get("turn"), int):
            raise WorkbenchError("Transcript has no reliable turn indices")
        if m["turn"] < turn or m["turn"] == turn and m["role"] == "user":
            messages.append({"role": m["role"], "content": m.get("text", "")})
    node = graph.nodes[node_id]
    prompt = compose_system_prompt_for_node(
        node=node,
        workflow=graph,
        has_recordings=False,
        format_prompt=lambda s: render_template(
            s, packet["run"].get("initial_context") or {}
        ),
    ).text
    context = LLMContext(messages=messages)
    context.set_tools(
        ToolsSchema(
            standard_tools=await compose_functions_for_node(
                node=node, custom_tool_manager=None
            )
        )
    )
    params = OpenAILLMAdapter().get_llm_invocation_params(
        context, system_instruction=prompt, convert_developer_to_user=True
    )
    # Convert optional provider sentinels into JSON omissions.
    request = {
        k: v
        for k, v in params.items()
        if isinstance(v, (dict, list, str, int, float, bool)) or v is None
    }
    historical_actions = len(transitions) > 1 or any(
        e.get("type")
        in {"function_call", "function_call_result", "tool_call", "tool_result"}
        and e.get("turn", turn) < turn
        for e in events
    )
    has_external_tools = any(
        n.tool_uuids or n.document_uuids for n in graph.nodes.values()
    )
    limitations = [
        "Reconstructed from frozen definition, recorded node entries and transcript; not a historical HTTP capture. Uses the current prompt composer."
    ]
    if historical_actions:
        limitations.append(
            "Historical tool messages are missing; this request cannot be replayed faithfully."
        )
    if has_external_tools:
        limitations.append(
            "Historical external-tool/knowledge context is unavailable; probe disabled."
        )
    safe = redact(request)
    return {
        "request": safe,
        "node_id": node_id,
        "node_name": node.name,
        "provenance": "reconstructed",
        "request_fingerprint": fingerprint(safe),
        "replayable": not historical_actions
        and not has_external_tools
        and safe == request,
        "limitations": limitations,
        "redacted": safe != request,
    }


class DiagnosticSession:
    def __init__(self, owner):
        self.owner = owner
        self.question = ""
        self.runs = set()
        self.reads = {}
        self.experiments = []
        self.requests = 0

    async def emit(self, kind, payload):
        owner = self.owner
        await db_client.wb_emit(
            owner.org,
            owner.session_id,
            kind,
            {**redact(payload), "job_id": owner.job["id"]},
            job_id=owner.job["id"],
            token=owner.job["lease_token"],
        )

    async def start(self, question, run_ids):
        if not question.strip() or not 1 <= len(run_ids) <= 5:
            raise WorkbenchError("Supply a diagnostic question and 1–5 run IDs")
        overviews = []
        for run_id in run_ids:
            overviews.append(await self.inspect(run_id))
        self.question = question
        self.runs.update(run_ids)
        previous = await db_client.wb_diagnostic_probes(
            self.owner.org, self.owner.session_id, run_ids
        )
        known = {e["evidence_id"] for e in self.experiments}
        for experiment in previous:
            packet = await self.packet(experiment["run_id"])
            if (
                experiment["evidence_id"] not in known
                and experiment.get("definition_fingerprint")
                == fingerprint((packet.get("definition") or {}).get("workflow_json"))
                and experiment.get("model_fingerprint")
                == fingerprint(packet["run"].get("model_configuration_snapshot"))
            ):
                restored = {
                    **experiment,
                    "reused": True,
                    "source_job_id": experiment.get("job_id"),
                }
                self.experiments.append(restored)
                known.add(restored["evidence_id"])
                await self.emit("diagnosis.probe.reused", restored)
        await self.emit("diagnosis.started", {"question": question, "run_ids": run_ids})
        return {
            "question": question,
            "runs": overviews,
            "previous_probes": [
                {k: v for k, v in e.items() if k not in {"baseline", "variant"}}
                for e in self.experiments
            ],
            "guidance": "Locate the first divergence. Inspect the effective request at relevant turns; form competing hypotheses; use paired probes where they can discriminate. Never change the Agent.",
        }

    async def packet(self, run_id):
        return await db_client.wb_diagnostic_run(
            self.owner.org, self.owner.session_id, run_id
        )

    async def inspect(
        self,
        run_id,
        section="overview",
        turn=None,
        request_index=0,
        offset=0,
        limit=16000,
    ):
        if section not in SECTIONS:
            raise WorkbenchError("Unknown diagnostic evidence section")
        packet = await self.packet(run_id)
        run, case = packet["run"], packet.get("case") or {}
        summary = execution_summary(case)
        evidence_id = f"run:{run_id}:{section}:{turn if turn is not None else 'all'}:{request_index}"
        metadata = {
            "evidence_id": evidence_id,
            "run_id": run_id,
            "section": section,
            "definition_id": run.get("definition_id"),
            "source": packet.get("source"),
            "execution": summary,
            "available_sections": list(SECTIONS),
        }
        if section == "overview":
            content = {
                "created_at": run.get("created_at"),
                "purpose": run.get("purpose"),
                "state": run.get("state"),
                "is_completed": run.get("is_completed"),
                "case_status": case.get("status"),
                "case_error": case.get("error"),
                "evaluation_evidence_available": bool(packet.get("source")),
                "recorded_runtime_context": redact(run.get("gathered_context") or {}),
                "model": (run.get("model_configuration_snapshot") or {})
                .get("services", {})
                .get("llm", {})
                .get("settings", {}),
                "turns": [
                    {
                        "turn": m.get("turn"),
                        "role": m["role"],
                        "text": m.get("text", "")[:350],
                    }
                    for m in case.get("transcript", [])
                ],
                "limitations": []
                if case
                else [
                    "Structured transcript/events unavailable. Read Agent Run artifacts; do not infer absence."
                ],
            }
        elif section == "transcript":
            content = [
                m
                for m in case.get("transcript", [])
                if turn is None or m.get("turn") == turn
            ]
        elif section == "evaluation":
            content = {
                "source": packet.get("source"),
                "result": {
                    key: value for key, value in case.items()
                    if key not in {"transcript", "events"}
                },
                "note": "Recorded evaluation outcome, checks, test context and simulation validity. Proposed replies with delivered=false were not sent to the Agent. Test assumptions are not business facts; evaluation errors are not Agent failures. Reviewer conclusions require checking the underlying evidence.",
                "limitations": [] if packet.get("source") else [
                    "No linked workbench evaluation was found for this run."
                ],
            }
        elif section == "execution":
            content = {
                "events": [
                    e
                    for e in case.get("events", [])
                    if e.get("type") not in {"bot_speech", "llm_request"}
                ],
                "run_gathered_context": run.get("gathered_context"),
                "case_gathered_context": case.get("gathered_context"),
                "run_logs": run.get("logs"),
                "note": "These are recorded runtime outputs. A visited-node list supports observed routing, but does not reveal an uncaptured model's internal reasoning or missing provider messages.",
            }
        elif section == "probes":
            content = [
                e
                for e in self.experiments
                if e["run_id"] == run_id and (turn is None or e["turn"] == turn)
            ]
        elif section == "definition":
            content = mask_workflow_definition(
                (packet.get("definition") or {}).get("workflow_json", {})
            )
        else:
            if turn is None:
                raise WorkbenchError("Request evidence needs an exact turn")
            try:
                content = await reconstruct_request(packet, turn, request_index)
            except WorkbenchError as error:
                content = {
                    "replayable": False,
                    "provenance": "unavailable",
                    "limitations": [str(error)],
                }
            metadata.update(
                {
                    k: content.get(k)
                    for k in (
                        "node_id",
                        "node_name",
                        "provenance",
                        "replayable",
                        "limitations",
                    )
                }
            )
        result = {**redact(metadata), **page(content, offset, limit)}
        previous = self.reads.get(evidence_id, {})
        ranges = sorted(
            previous.get("ranges", [])
            + [[offset, min(offset + limit, result["characters"])]]
        )
        through = 0
        for start, end in ranges:
            if start > through:
                break
            through = max(through, end)
        self.reads[evidence_id] = {
            **redact(metadata),
            "content": redact(content),
            "ranges": ranges,
            "complete": through >= result["characters"],
        }
        await self.emit("diagnosis.evidence", result)
        return result

    def record_source(self, source):
        """Original-source citations are valid only after their actual pages were read."""
        evidence_id = source["id"]
        previous = self.reads.get(evidence_id, {})
        pages = previous.get("content", {}).get("pages", [])
        if source not in pages:
            pages = [*pages, copy.deepcopy(source)]
        self.reads[evidence_id] = {
            "evidence_id": evidence_id,
            "run_id": None,
            "section": "source",
            "provenance": "original_source",
            "title": source.get("title"),
            "content": {"pages": pages},
        }
        return evidence_id

    async def probe(
        self,
        run_id,
        turn,
        hypothesis,
        prediction,
        node_id=None,
        edits=None,
        repeats=2,
        request_index=0,
    ):
        if not self.question or run_id not in self.runs:
            raise WorkbenchError("Start diagnosis for this run before probing")
        if not hypothesis.strip() or not prediction.strip() or not 1 <= repeats <= 3:
            raise WorkbenchError(
                "Specify hypothesis, predicted difference and 1–3 paired repetitions"
            )
        if not node_id and not edits:
            raise WorkbenchError(
                "Provide one temporary intervention to compare with the unchanged baseline"
            )
        if node_id and edits:
            raise WorkbenchError("Change only one factor: node context OR prompt text")
        if self.requests + repeats * 2 > 24:
            raise WorkbenchError(
                "Diagnostic request budget reached (24); report remaining hypotheses as untested"
            )
        packet = await self.packet(run_id)
        if not packet.get("definition"):
            raise WorkbenchError(
                "A frozen definition is required for a controlled probe"
            )
        baseline = await reconstruct_request(packet, turn, request_index)
        if not baseline["replayable"]:
            raise WorkbenchError(
                "Cannot faithfully replay this historical request: "
                + " ".join(baseline["limitations"])
            )
        from pipecat.adapters.schemas.tools_schema import ToolsSchema
        from pipecat.processors.aggregators.llm_context import LLMContext

        from api.services.configuration.model_connections import (
            hydrate_model_configuration_snapshot,
        )
        from api.services.pipecat.service_factory import create_llm_service
        from api.services.workflow.dto import ReactFlowDTO
        from api.services.workflow.pipecat_engine_context_composer import (
            compose_functions_for_node,
            compose_system_prompt_for_node,
        )
        from api.services.workflow.workflow_graph import WorkflowGraph
        from api.utils.template_renderer import render_template

        snapshot = packet["run"].get("model_configuration_snapshot")
        if not snapshot:
            raise WorkbenchError(
                "No frozen model configuration; refusing to substitute a current model"
            )
        config = await hydrate_model_configuration_snapshot(self.owner.org, snapshot)
        if config.llm.provider not in {"openai", "openrouter", "atlascloud", "hopper"}:
            raise WorkbenchError(
                "Isolated probes currently support OpenAI-compatible text models; this provider remains inspect-only"
            )
        if getattr(getattr(config, "llm_fallback", None), "rules", None):
            raise WorkbenchError(
                "Historical fallback route is ambiguous; inspect-only until the executed model route can be reproduced"
            )
        captured_model = baseline["request"].get("model")
        if captured_model and captured_model != config.llm.model:
            raise WorkbenchError(
                "Captured model differs from the frozen model configuration; refusing to substitute a model"
            )
        variant = copy.deepcopy(baseline["request"])
        graph = WorkflowGraph(
            ReactFlowDTO.model_validate(packet["definition"]["workflow_json"]),
            skip_instance_constraints_for={"trigger"},
        )
        if node_id:
            if (
                node_id not in graph.nodes
                or graph.nodes[node_id].tool_uuids
                or graph.nodes[node_id].document_uuids
            ):
                raise WorkbenchError(
                    "Use an existing node without external tools/knowledge dependencies"
                )
            node = graph.nodes[node_id]
            prompt = compose_system_prompt_for_node(
                node=node,
                workflow=graph,
                has_recordings=False,
                format_prompt=lambda s: render_template(
                    s, packet["run"].get("initial_context") or {}
                ),
            ).text
            systems = [m for m in variant["messages"] if m.get("role") == "system"]
            if len(systems) != 1:
                raise WorkbenchError(
                    "Cannot isolate node context in a request with multiple/missing system messages"
                )
            systems[0]["content"] = prompt
            c = LLMContext()
            c.set_tools(
                ToolsSchema(
                    standard_tools=await compose_functions_for_node(
                        node=node, custom_tool_manager=None
                    )
                )
            )
            # Obtain provider tool conversion without running a pipeline or invoking handlers.
            from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter

            converted = OpenAILLMAdapter().get_llm_invocation_params(
                c, convert_developer_to_user=True
            )
            variant["tools"] = (
                converted["tools"] if isinstance(converted["tools"], list) else []
            )
        else:
            if not 1 <= len(edits) <= 3:
                raise WorkbenchError("Supply 1–3 exact prompt edits")
            systems = [m for m in variant["messages"] if m.get("role") == "system"]
            if len(systems) != 1 or not isinstance(systems[0].get("content"), str):
                raise WorkbenchError(
                    "A single text system instruction is required for prompt intervention"
                )
            for edit in edits:
                if not edit["old"] or systems[0]["content"].count(edit["old"]) != 1:
                    raise WorkbenchError(
                        "Prompt edit must match exactly once in the effective system instruction"
                    )
                systems[0]["content"] = systems[0]["content"].replace(
                    edit["old"], edit["new"], 1
                )
        if variant == baseline["request"]:
            raise WorkbenchError("Intervention does not change the request")
        experiment = {
            "evidence_id": "probe:" + str(uuid.uuid4()),
            "run_id": run_id,
            "turn": turn,
            "hypothesis": hypothesis,
            "prediction": prediction,
            "node_id": node_id,
            "edits": edits or [],
            "repeats": repeats,
            "model": config.llm.model,
            "model_fingerprint": fingerprint(snapshot),
            "definition_fingerprint": fingerprint(
                packet["definition"]["workflow_json"]
            ),
            "history_fingerprint": fingerprint(
                [m for m in variant["messages"] if m.get("role") != "system"]
            ),
            "provenance": baseline["provenance"],
            "limitations": baseline["limitations"]
            + [
                "A probe is one model completion; returned tool calls are recorded but never executed. It is not a full flow test.",
                "Node intervention changes that node's prompt and tool set together; it does not isolate their individual effects.",
            ]
            if node_id
            else baseline["limitations"]
            + ["Returned tool calls are never executed; not a full flow test."],
            "baseline": baseline["request"],
            "variant": variant,
            "results": [],
            "agent_modified": False,
        }
        self.experiments.append(experiment)
        await self.emit(
            "diagnosis.probe.started",
            {k: v for k, v in experiment.items() if k not in {"baseline", "variant"}},
        )
        for index in range(repeats):
            for label, request in (
                ("baseline", baseline["request"]),
                ("variant", variant),
            ):
                await db_client.wb_heartbeat(
                    self.owner.org,
                    self.owner.session_id,
                    self.owner.job["id"],
                    self.owner.job["lease_token"],
                )
                self.requests += 1
                service = create_llm_service(config)
                try:
                    # Same frozen provider settings for both arms; no caller-selected URL/key/model.
                    params = completion_params(service, request, baseline["provenance"])
                    result = await asyncio.wait_for(
                        collect_completion(service, params), 60
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - record a bounded probe failure, never provider secrets
                    result = {
                        "error": type(error).__name__,
                        "text": "",
                        "tool_calls": [],
                    }
                finally:
                    await service._client.close()
                row = {"arm": label, "repeat": index + 1, **result}
                experiment["results"].append(row)
                await self.emit(
                    "diagnosis.probe.progress",
                    {
                        "evidence_id": experiment["evidence_id"],
                        "run_id": run_id,
                        "turn": turn,
                        **row,
                    },
                )
        await self.emit("diagnosis.probe.completed", experiment)
        return redact(experiment)

    def receipt(self):
        return {
            "question": self.question,
            "run_ids": sorted(self.runs),
            "reads": list(self.reads.values()),
            "experiments": redact(self.experiments),
            "model_requests": self.requests,
            "reused_probe_count": sum(bool(e.get("reused")) for e in self.experiments),
            "agent_modified": False,
        }

    def validate_report(self, report):
        if not report:
            raise WorkbenchError(
                "Diagnosis requires a structured report with evidence, hypotheses and limitations"
            )
        available = set(self.reads) | {e["evidence_id"] for e in self.experiments}
        for item in report["findings"]:
            if not item["evidence_ids"] or not set(item["evidence_ids"]) <= available:
                raise WorkbenchError(
                    "Every finding must cite inspected evidence or a real probe receipt"
                )
        if report["status"] == "concluded":
            if not any(f["status"] == "supported" for f in report["findings"]):
                raise WorkbenchError(
                    "A concluded diagnosis needs a supported causal finding"
                )
            for run in self.runs:
                if not any(
                    r["run_id"] == run and r["section"] == "request" and r["complete"]
                    for r in self.reads.values()
                ):
                    raise WorkbenchError(
                        "Inspect complete effective request evidence for every diagnosed run before concluding; follow next_offset"
                    )


class DiagnosticReviewer:
    """The verifier can independently fetch source pages, but never run probes/edit."""

    def __init__(self, owner):
        self.owner = owner

    def specs(self):
        owner = self.owner
        specs = owner.specs()
        source = next(s for s in specs if s["name"] == "read_task_basis")
        inspect = owner.diagnostic_actions["inspect_run_diagnostic"]
        # StructuredDecision's completion spec uses the legacy app-server shape.
        # A catalog may not mix that shape with canonical type=function entries.
        return [
            {k: v for k, v in spec.items() if k != "type"} for spec in (source, inspect)
        ]

    async def call(self, name, arguments):
        import jsonschema

        specs = {s["name"]: s for s in self.specs()}
        if name not in specs:
            raise WorkbenchError("Diagnosis reviewer permits evidence reads only")
        jsonschema.validate(arguments, specs[name]["inputSchema"])
        if name == "read_task_basis":
            return await self.owner.read_task_basis(**arguments)
        if arguments["run_id"] not in self.owner.diagnostics.runs:
            raise WorkbenchError("Reviewer run is outside this diagnosis")
        return await self.owner.inspect_run_diagnostic(**arguments)


def completion_params(service, request, provenance):
    if provenance != "captured":
        return service.build_chat_completion_params(request)
    # A capture is the HTTP JSON body, not SDK keyword arguments. Preserve it
    # verbatim through extra_body, including provider extensions unknown to the
    # SDK and the actual historical sampling settings. The frozen client still
    # supplies credentials and the endpoint; neither comes from the capture.
    return {
        "model": request["model"],
        "messages": copy.deepcopy(request["messages"]),
        **({"stream": request["stream"]} if "stream" in request else {}),
        "extra_body": copy.deepcopy(request),
    }


async def collect_completion(service, params):
    stream = await service._client.chat.completions.create(**params)
    if not params.get("stream"):
        message = stream.choices[0].message
        return {
            "text": message.content or "",
            "tool_calls": [
                {"name": call.function.name, "arguments": call.function.arguments}
                for call in (message.tool_calls or [])
                if call.type == "function"
            ],
        }
    text, calls = "", {}
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text += delta.content or ""
            for call in delta.tool_calls or []:
                entry = calls.setdefault(call.index, {"name": "", "arguments": ""})
                if call.function:
                    entry["name"] += call.function.name or ""
                    entry["arguments"] += call.function.arguments or ""
    finally:
        await stream.close()
    return {"text": text, "tool_calls": list(calls.values())}


def install_request_capture(service, engine, emit):
    """Observe evaluation HTTP bodies only; do not change requests or credentials."""
    client = getattr(service, "_client", None)
    http = getattr(client, "_client", None)
    if http is None or not hasattr(http, "event_hooks"):
        return False

    async def capture(request):
        if request.method != "POST" or not request.url.path.endswith(
            "/chat/completions"
        ):
            return
        metadata = {}
        try:
            node = engine.active_agent.current_node
            metadata = {
                "node_id": node.id if node else None,
                "node_name": node.name if node else None,
            }
            if len(request.content) > 500_000:
                emit(
                    "llm_request",
                    {
                        **metadata,
                        "redacted": True,
                        "limitations": [
                            "Request exceeds capture size limit; no replayable payload stored."
                        ],
                    },
                )
                return
            body = json.loads(request.content)
            safe = redact(body)
            encoded = json.dumps(safe, ensure_ascii=False)
            secret = getattr(client, "api_key", None)
            if isinstance(secret, str) and secret:
                encoded = encoded.replace(secret, "[redacted]")
            safe = json.loads(encoded)
            emit(
                "llm_request",
                {
                    **metadata,
                    "request": safe,
                    "redacted": safe != body,
                    "request_fingerprint": fingerprint(safe),
                    "limitations": [],
                },
            )
        except Exception:  # noqa: BLE001 - passive evidence capture must never break model execution
            try:
                emit(
                    "llm_request",
                    {
                        **metadata,
                        "redacted": True,
                        "limitations": [
                            "Request capture unavailable; no replayable payload stored."
                        ],
                    },
                )
            except Exception as error:  # noqa: BLE001 - a failed evidence sink is not a model failure
                from loguru import logger

                logger.debug(
                    "Diagnostic capture sink unavailable: {}", type(error).__name__
                )

    http.event_hooks.setdefault("request", []).append(capture)
    return True

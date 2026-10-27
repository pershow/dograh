"""Workbench authoring defaults, not workflow topology or runtime constraints."""

from api.services.workflow.workflow_graph import transition_tool_name

OPENING_GUIDANCE = """For source-defined fixed openings, or openings expressible with existing
template variables, prefer the existing startCall greeting_type="text" and greeting
fields instead of asking the LLM to generate the same line from its prompt. The
supported syntax is {{variable}} / {{nested.path}} and {{variable | fallback:neutral
wording}}. Choose a business-appropriate neutral fallback for missing, null or empty
values; do not invent an identity or speak an empty placeholder. Do not use Jinja
conditionals, default() or invented filters. Register unknown variables without fake
defaults. Whitespace-only values are not normalized by this renderer; if that input
is possible, use a source-appropriate opening independent of that value rather than
promising unsupported template logic.
Preserve explicit recorded openings (greeting_type="audio", greeting_recording_id),
dynamic openings requiring live judgment, caller-first/delayed-start settings and
other user requirements. In local edits retain unrequested settings. Cascade speech
can speak configured text directly; realtime model adapters may still generate it.
Do not promise LLM bypass or a latency SLA for unsupported modes. Use existing opening
delivery/history handling, not custom runtime changes or extra assistant messages.
The opening occurs once: subsequent prompts recognize its delivered history, retain
all facts in the first customer reply and hand them to the next applicable node.
"""

TOOL_SCOPE_GUIDANCE = """工具作用域约束：业务规则、停止条件和优先级不授予工具权限。只调用当前请求实际公布的工具；其他节点的出边、业务工具和资料里提到的工具不会因此可用。
当前节点的工具有多个来源：真实出边生成的路由工具、绑定的业务工具及其他已公布能力。tool_uuids只描述业务绑定，不是完整工具列表，不能据此排除本节点已有的结束或交接出边；当前实际公布的列表优先于提示中不完整的工具描述。
一次只调用当前节点的一个路由工具，等待交接结果及目标节点新公布的工具列表后，再决定下一次调用。不能在同一批或并行调用中预先执行下一节点的工具；已经完成的路由不再次调用，结束节点也不重复调用历史中的结束工具。
全局层只定义共享规则及适用条件，不无条件命令所有节点调用某个节点专属的工具。节点路由指令使用真实图的工具名，不把示例名称当作通用名称。
结束条件满足且当前提供适用结束路由时，本轮调用该路由。若当前没有结束工具，沿真实图中可用且适用的交接路由静默传递完整历史及待执行的结束要求，由拥有结束能力的节点立即处理；不重复介绍、不额外确认、不继续挽回，不编造工具，也不只口头告别冒充已结束。
典型开始节点仅有通向业务节点的路由时，首条回复即使要求停止，也先调用当前提供的该路由。多业务节点按各自真实出边与工具范围传递；若图中没有可行结束路径，应在构建/复核中报告缺口，不臆造连接或承诺挂机。
以上同样适用于拒绝上限、重试上限、异常和取消；全局或原始SOP中的结束要求不能越过当前工具范围。这是对话提示，不是硬性状态机保证。
"""


def node_routing_context(graph, node):
    """Describe this node's real outgoing tools without granting new capabilities."""
    if node["type"] == "globalNode":
        return "全局节点无独立路由工具；共享规则随实际执行节点的工具范围应用。"
    if node["type"] == "endCall":
        return "当前为结束节点，只执行适用的结束话术，不重新路由到业务步骤。"
    names = {n["id"]: n["data"]["name"] for n in graph.get("nodes", [])}
    routes = [
        f"{transition_tool_name(e['data']['label'])} → {names.get(e['target'], e['target'])}"
        for e in graph.get("edges", [])
        if e["source"] == node["id"]
    ]
    return (
        "本节点图中出边工具："
        + ("；".join(routes) if routes else "无")
        + "。适用条件按真实出边执行；实际可调用范围以当前公布的工具列表为准，业务/知识工具也不例外。"
    )


STRUCTURE_GUIDANCE = """For a new Agent or a user-requested redesign, prefer four physical nodes:
one startCall for the opening, one agentNode for the complete business conversation,
one endCall for closing, and one globalNode for shared rules. Organize SOP phases,
questions and branches as internal stages of the main Agent, not one node per heading
or question. Multiple distinct terminal conditions can use separately labelled edges
to the SAME endCall; retain each condition and its applicable closing wording.
Explicit user requirements take precedence: honor requested node counts, topology,
independent nodes and special capabilities. Retain necessary tools, side effects,
integrations, error branches and confirmation boundaries, adding physical nodes when
their execution requires them. Never replace a tool/side effect with a prompt claim
or discard business semantics to reach four nodes. Explain justified deviations briefly;
four is a preference, never a save/validation/acceptance gate.
For a local edit of an existing Agent, preserve its topology and unrelated behavior.
Do not redesign it to match this default. Automatic failure repair is also a local edit.
On an explicitly requested restructure, use SDK edits and exact removals to migrate
the graph while preserving the source requirements, tools and observable behavior.
Keep shared rules consistent with local prompts. A globalNode has no edges; explicitly
enable add_global_prompt on prompted nodes that should receive its rules, including
endCall when those rules apply there. Do not let shared continuation rules override
an authorized terminal condition. The end node closes; it must not reopen the SOP.
In the main Agent prompt, name each actual terminal transition tool and its precise
condition, scoped to the node that actually exposes it. When a terminal condition
holds, use that node's available exit, or silently hand off the pending stop along
an applicable real route to the node with that exit;
saying goodbye or reporting success does not transition or hang up. Put closing
speech in endCall, and do not add a wait for the customer to acknowledge a tool result
unless the source requires it. Do not make entry to endCall depend on speech that
the endCall itself is responsible for producing.
Compile authorized stop/limit/exception rules into an explicit priority list BEFORE
the normal conversation stages. For each, state the source's trigger/evidence,
counting scope and the applicable current-node exit or handoff. Do not accidentally
nest a conversation-wide stop under a later tool-execution stage: a reported manual
attempt or failure can satisfy a source-defined stop without that tool having run.
Preserve the source's actual scope and evidence requirements; do not add new exits.
Internal prompt stages are conversation guidance, not physical node transitions or
a hard state-machine guarantee. The workflow runtime and SDK semantics are unchanged.
Keep the startCall opening small: name its actual handoff tool and invoke it once
the source's opening condition holds. Carry the customer's first reply and all its
facts into the main Agent; do not repeat the greeting or restart information collection.
For a fixed spoken opening, set startCall.greeting to the requested text. A prompt
reference does not change that runtime field. When editing an opening, update the
greeting field and any conflicting authored prompt reference, preserving other nodes.
"""
STRUCTURE_GUIDANCE += OPENING_GUIDANCE + TOOL_SCOPE_GUIDANCE

FACT_CONTINUITY_GUIDANCE = """追问事实前，在内部核对两份清单，不对客户朗读清单或内部规则：
已发生的操作及结果也是已知事实。按原文区分首次尝试与重试；用户自行完成并报告的重试同样计入，不以助手是否刚刚要求该操作为门槛。不能因为助手之前漏处理失败、误问资料或尚未进入某个内部阶段，就把用户已完成的尝试归零或要求重做。先据完整历史检查适用的停止上限，再决定是否还有应追问或重试的步骤。
要求清单：仅列适用 SOP 或真实工具定义明确要求的事实、来源依据及明确规定的格式/精度。未规定约束就记为无额外格式或精度约束，不能用业务常识补上要求。
已有事实：从完整对话提取每项已提供的值，包括触发节点跳转的客户回复。切换节点不清空事实，不代表开始新通话。
只有要求清单中的事实确实没有值、明确违反该项已有约束，或存在妨碍该要求的未解决矛盾，才允许追问；问题仅涉及这个缺失项或冲突部分。
用户已经给出的描述就是该字段的已知值。字符串类型本身不要求标准格式、精确数值、日期或起止范围；没有明确约束时直接保留原话，不把该值拆成新的缺失子项。模型希望得到更详细的信息不等于业务要求它。
原文“收集/确认”若干字段是在定义所需信息，不是每次必须照读的提问脚本。缺一项不使其他项变为缺失，不能重问整套资料。
若 SOP 或工具明确要求更高精度/格式，只澄清不满足约束的部分，保留其他事实，不编造值来满足格式。
新的核对确认与信息采集分开：先说明并复述已有值，再请求客户确认这份方案，不要求重新提供这些值。客户修改一项只更新该项及依赖它的确认，不清空其他信息和已完成步骤。
没有必需的缺失事实时，推进适用的说明、确认、工具或跳转，不再发明采集问题。
"""

CONVERSATION_GUIDANCE = (
    FACT_CONTINUITY_GUIDANCE
    + """Compile the source into an explicit internal conversation plan:
In the authored business prompt, enumerate the required facts and, for each, its
source/tool basis and acceptance constraints. If the source/schema specifies no
format or precision constraint, explicitly mark it as accepting the customer's
description without further precision questions. Resolve this separately for EACH
field at build time: write its actual constraint or "none specified". Do not copy
conditional wording such as "if no precision is required" into the fact list, leaving
the runtime to decide whether a constraint exists. Do not leave this to domain
assumptions at runtime. Do not invent defaults or fill this plan with test values.
Keep the requirement list separate from conversational wording: at each turn,
subtract facts already supported by the history before choosing a question. Missing
one fact does not make any other supplied fact missing. Required confirmation
repeats the proposal for approval, never the original blank intake questionnaire.
track known facts, applicable prerequisites, completed operations, pending questions,
required disclosures/confirmations, and tool outcomes. A single customer reply can
report several completed steps: absorb ALL supported facts together and ask only for
missing prerequisites. Do not infer earlier completion, consent or tool success from
a later result; do not treat a vague acknowledgment as a specific missing fact.
Accept supplied facts at the granularity the source and tool schema require; do not
invent stricter formats, precision, extra fields or alternative procedures. Clarify
only when an actual prerequisite is absent or materially ambiguous for this task.
Honor source-required fresh confirmation AFTER its disclosure/question; a report of
prior operations cannot confirm a question not yet asked. Do not add new confirmation
requirements or force repeat operations when the source permits continuation.
Answer side questions and resume the actual unfinished requirement. If the same reply
also reports progress, update it. On changes of device, state, channel or other relevant
facts, continue from the latest supported situation, retaining completed work; clarify
material contradictions instead of replaying old navigation or silently overwriting facts.
Distinguish an ordinary information question from a refusal using the user's meaning
and reason, not isolated keywords or the assistant's chosen response template.
If the SOP has retry/retention limits, derive the limit, counting unit, scope, reset
conditions and terminal action from that SOP. Count actual attempts in full history,
pairing the user's applicable reason/step with the assistant's actual intervention;
a misclassified persuasion attempt still counts when it meets the source definition.
Side questions, device changes or branch switching do not by themselves reset counts.
At each turn, update facts and counts first, then check source-defined stop/limit
conditions before choosing the next unfinished step. Once a terminal limit is reached,
take its exit without an extra retry, persuasion or invented alternative method;
ordinary continuation and updated-state rules do not override that terminal condition.
Do not invent a universal limit, device field, price, fee or mandatory disclosure.
Success termination requires every applicable prerequisite, required tool result and
final confirmation; intermediate progress is insufficient. Keep source-authorized
refusal, cancellation, failure and other exits separate from successful completion.
"""
)

BEHAVIOR_TEST_GUIDANCE = """Verify internal business stages with turn-specific content,
context and tool assertions, not invented node visits. Node sequence assertions only
describe actual physical transitions; visiting the main Agent or ending a call does
not prove completion of its internal SOP. Use source-derived multi-turn cases for
combined progress, missing prerequisites, side questions followed by continuation,
updated state/device, questions versus refusals, limit boundaries across interruptions
and branch switches, required tool success/failure, and final confirmation, when
applicable. Use the SOP's own values and counting rules, not example-domain constants.
For missing-fact cases, check that known facts are not requested again and that no
unsupported precision is added. Also cover a source/tool that explicitly requires
precision: the insufficient part must be clarified, without repeating other facts.
Do not require four nodes for explicit alternate structures or existing local edits.
Do not treat prompt instructions as deterministic runtime enforcement or a passing
text simulation as proof of live voice behavior.
Check configured fixed/variable openings, absent/null/empty template values, explicit
recorded/dynamic openings and first-reply cancellation. Verify actual completion and
each request's advertised tools across node transitions; a spoken goodbye is not
termination. Text/configuration checks do not establish real audio playback or latency.
"""

# Syntax example only. Business prompts and terminal conditions must be compiled
# from the user's actual source; this is never installed as a runnable default Agent.
SDK_EXAMPLE = """import { Workflow } from "@dograh/sdk";
import { startCall, agentNode, endCall, globalNode } from "@dograh/sdk/typed";
const wf = new Workflow({ name: "Agent" });
const start = wf.addTyped(startCall({ name: "Opening", greeting_type: "text", greeting: "Hello, how can I help?", prompt: "The configured opening is delivered by the existing greeting mechanism. Do not repeat it. Once the source opening condition holds, or the customer requests a stop, silently call begin_conversation with the full history and all facts or pending stop requirements. Only use tools currently provided; this node does not expose the Closing routes.", add_global_prompt: true }));
const main = wf.addTyped(agentNode({ name: "Conversation", prompt: "Handle the complete source-defined SOP as internal stages. Update facts and actual attempt counts before deciding the next step. Check terminal limits before continuing. Ask only for missing prerequisites and satisfy required tool outcomes and confirmations. When success conditions hold call finish_success now; for an authorized other stop call finish_other now. The Closing node delivers the closing speech; do not merely report completion or invent another wait or retry.", add_global_prompt: true }));
const end = wf.addTyped(endCall({ name: "Closing", prompt: "Use the source-defined closing appropriate to the actual outcome. Do not restart the business conversation or claim success after cancellation/failure.", add_global_prompt: true }));
const rules = wf.addTyped(globalNode({ name: "Shared rules", prompt: "Follow the supplied business facts and conditions. Retain supported progress. Stop conditions take priority over introductions or persuasion. Only call currently provided tools: if an applicable ending tool is unavailable here, silently hand off the full pending stop through an available applicable route to the node that can end. Do not invent tools or merely say goodbye. Answer side questions and resume only unfinished requirements while active." }));
wf.edge(start, main, { label: "begin_conversation", condition: "The source-defined opening is complete, or the customer requests a stop; continue with the full history and any pending stop without further introduction or confirmation." });
wf.edge(main, end, { label: "finish_success", condition: "All source-defined success prerequisites, required tool results and final confirmations are satisfied." });
wf.edge(main, end, { label: "finish_other", condition: "A source-authorized cancellation, refusal, failure or other stop condition is satisfied. This is not successful completion." });"""


def authoring_context(graph, *, automatic_repair=False):
    """Expose scope without guessing user intent from node counts or keywords."""
    return {
        "scope": (
            "local_repair"
            if automatic_repair
            else "existing_agent" if graph.get("nodes") else "new_agent"
        ),
        "structure": STRUCTURE_GUIDANCE,
        "conversation": CONVERSATION_GUIDANCE,
        "verification": BEHAVIOR_TEST_GUIDANCE,
    }

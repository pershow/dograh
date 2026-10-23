# Jev node routing

Jev can decide whether a user's reply satisfies an outgoing edge before the
conversational LLM runs. A confident transition uses the existing engine handler
(including variable extraction, transition speech and end-node playback), then
asks the LLM for the destination node's response. A confident stay generates a
response at the current node. Transition tools are hidden for that first
generation; the canonical context keeps them for later turns. Business tools
remain available and can still require their own follow-up generations.

## Configuration

Set `TYPESAFE_API_KEY` in the backend environment. Local processes read `api/.env`;
Docker Compose passes the root environment value into the API and its workers.
For Helm, use `secrets.typesafeApiKey`, or include `TYPESAFE_API_KEY` in the Secret
selected by `secrets.existingSecret`. The key is never stored in a workflow.

In **Settings → General → Jev Node Routing**, choose a mode and save/publish the
agent. Every run and transferred agent uses its pinned definition's settings.
Equivalent workflow configuration:

```json
{
  "node_routing": {
    "mode": "off",
    "model": "jev-1.13.0",
    "confidence_threshold": 0.85,
    "timeout_ms": 600
  }
}
```

| Mode | Behavior |
| --- | --- |
| `off` (default) | Original LLM/tool-calling flow; no Jev request. |
| `shadow` | Original flow proceeds immediately; Jev records a background prediction. |
| `on` | Apply confident decisions; otherwise forward the original request to the LLM. |

Connections are prewarmed in the background during pipeline setup and reused
across text turns and calls in the same worker. Cold connection waits still count
against the decision timeout, but a timed-out turn does not cancel shared warmup.
HTTP classification retries are disabled. Missing credentials, service errors, invalid results, low confidence
and an explicit `uncertain` choice all retain the original flow. Cancellation
propagates; a result from an inactive agent or outdated context cannot switch a
node. Realtime speech-to-speech calls keep their native flow. Unsupported
provider-specific history, over 253 outgoing edges, or more than 32,000
characters of decision evidence also use the original flow, without truncating
the evidence to force a decision.

The implementation uses Pipecat's existing `JevClassifier` and hosted `JevClient`.
It does not run a local AnyJev model. Processors release their connection leases
at cleanup. The worker closes unused pools after 240 seconds or at API shutdown;
only the HTTP transport is shared, never conversation history or decisions.

## Comparing results

Langfuse receives a `jev-node-routing` span under the same conversation/turn.
It records classification outcome, model, confidence, threshold and classification
latency, with the actual classification input and decision output. Connection wait,
inference time and failure stage distinguish cold connections from slow inference.
It does not add a second conversational LLM generation. Application-issued
transition history uses tool-call IDs prefixed with `jev-route-`.

The last 200 decisions are also saved in `gathered_context.node_routing`. Records
contain source/predicted node IDs, mode, outcome, confidence, timeout and latency.
The applied outcome is `transition` or `stay`; fallback reasons include
`low_confidence`, `uncertain`, `timeout`, `missing_api_key`, `error`, `stale` and
`unsupported_context`. A cancelled background observation is marked `cancelled`.

For shadow runs, the next user turn or pipeline cleanup records the original
LLM's resulting node as `llm_target_node_id`, with `agrees_with_llm` when both
decisions are available. This is agreement with the existing flow, **not an
accuracy label**. Agent transfers and incomplete/cancelled turns should be
excluded from comparisons.

Use the same pinned graph, conversational model and labeled dialogue cases for
off/shadow/on runs. Compare end-to-end turn latency (p50/p95), transition accuracy
against human-reviewed target nodes, and fallback rate. Include timeout overhead
in latency: an unsuccessful enabled decision can add up to the configured budget
before the original model runs. Chinese dialogue accuracy and real service
latency require evaluation with the deployment's TypeSafe credentials; mocked
regression tests only verify routing, fallback and lifecycle behavior.

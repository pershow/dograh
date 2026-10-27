# Agent Workbench prototype

This is the first, UI-design stage of the Codex Agent Builder MVP. It is a
standalone interactive prototype, not a production Agent Builder replacement.
It uses the existing Dograh branding and requires no frontend dependencies.

## Preview

From the repository root:

```bash
python3 -m http.server 4177 --bind 127.0.0.1 --directory ui/public
```

Open `http://127.0.0.1:4177/prototypes/agent-workbench/`.
The same path works through the normal Next.js frontend.

The example Agent data and file metadata are stored in this browser's localStorage under
`dograh-workbench-prototype-v1`. Reset removes only this prototype's state.
User-selected file bytes remain in memory in the current tab and are never uploaded.
There are no model calls, audio capture, audio playback, backend requests, or
production workflow changes. Role switching is an interaction preview, not
authentication or authorization. Do not use it as a security boundary.

## Reviewable interactions

- Existing Agent: canvas, selected node, editable prompt, model presets,
  variables, and tool catalogue. The shell follows the repository's
  `WorkflowEditorHeader`, `EditorSetup`, and `WorkflowTesterPanel`: black header,
  floating Canvas / Model / Variables / Settings switcher, dotted canvas,
  Test Audio / Test Chat rail. The workbench adds an assistant rail and
  simulation/tuning views to that layout.
- New Agent: name, requirement input, example prompts, staged construction.
  Construction always loads the appointment example; it does not generate
  arbitrary flows from the requirement yet.
- Attachments in both the construction form and ongoing assistant conversation:
  choose multiple files, drag into either composer, paste image files, preview,
  remove, select purpose, send with text or a simulated voice instruction, and
  reuse a previously sent reference. Up to 5 files per message, 20 MB each.
  TXT/MD/CSV/JSON have escaped plain-text previews (first 24 KB); raster images
  have local previews; PDF/DOCX/XLSX show file metadata only. No document parsing
  or extraction has been implemented. Reload preserves metadata but requires
  reselecting pending files before submission; sent references remain visible.
- Text simulation: select scenarios, configure turn limits, run/cancel a batch,
  inspect conversations and tool parameters. The initial fixture passes 4/6.
- Tuning: inspect two failures, generate a candidate, compare node prompts,
  run a 6/6 fixture regression, adopt the candidate, and preview publication.
- Version identity: adopting a candidate does not publish it. Manual changes
  invalidate previous evaluation results. Uncommitted prompt edits survive
  navigation and block evaluating a different saved version accidentally.
- Voice collaboration: scripted captions and command buttons, listening /
  replying / muted / disconnected states, interrupt reply, cancel task, end
  voice session, and reconnect. Ending voice does not cancel a running task.
- Multiple local example Agents, independent conversations, reload recovery,
  admin/editor/viewer interaction previews, and small screens.

The prototype intentionally evaluates fixtures rather than user-authored
prompts. A real evaluator must replace this behavior before product release.

## Implementation decision: internal tools first

MCP is a transport and discovery boundary; it does not determine the UI.
For this embedded workbench, put operations in a transport-independent Dograh
service layer and expose them to Codex through an adapter. Keep the existing
MCP endpoints as another adapter over the same services for external clients.
Do not duplicate graph validation, organization authorization, or transactions.

Suggested operation catalogue:

| Operation | Responsibility |
| --- | --- |
| `get_agent_context` | Read the authorized Agent, frozen definition, selected node, available tools, and relevant reports. |
| `validate_candidate` | Parse workflow source and validate graph, model, variable, tool, and trigger references. |
| `save_candidate` | Create an immutable candidate with its full behavior configuration, expected base revision, and idempotency key. |
| `save_eval_suite` | Create a versioned set of scenarios, assertions, judge configuration, and tool fixtures. |
| `start_evaluation` | Enqueue a bounded evaluation against an exact candidate and suite version. Return its job ID. |
| `get_evaluation` | Read report summaries and paginated per-case evidence. |
| `compare_evaluations` | Compare runs with compatible scenario and scoring configurations. |
| `cancel_job` | Cancel only a job the acting principal is allowed to control. |

The adapter derives organization/user/session scope from authenticated server
context, never from untrusted tool arguments. Workflow adoption and publication
use authorized backend commands and optimistic concurrency checks.

Codex app-server supports client-executed dynamic tools using `dynamicTools`
on `thread/start` and the `item/tool/call` request/response flow. These APIs are
**experimental**. Pin the runtime version, generate its schemas, test the
adapter contract, and keep this dependency behind a small interface. The
internal tool service remains reusable if a different transport is required.
Use a server-owned local process/stdio connection rather than exposing a raw
Codex listener to browsers. Do not share unrestricted thread lookup or process
state across tenants.

Source: https://learn.chatgpt.com/docs/app-server

## Voice architecture for the actual MVP

```text
Browser microphone and speaker
  ↕ WebRTC voice session
Realtime conversational layer
  ↕ submit_instruction / get_job_status / cancel_job
Dograh Workbench session and task services
  ↔ Codex adapter → internal Dograh tools
  ↔ evaluation workers → actual Dograh workflow runtime
  → persisted events → UI stream and voice result summaries
```

The realtime layer handles conversational turn taking, audio, transcripts, and
interruptions. Codex performs longer construction and tuning tasks. It should
not be necessary to wait for Codex's full reasoning turn to keep speaking.

- Backend-created short-lived voice credentials; never send long-lived model
  credentials to the browser.
- Text and voice commands bind to the same authorized workbench session,
  selected definition, and task revision. A transcript alone is not trusted
  authorization to bypass role restrictions or publish a different version.
- Committed utterances produce one idempotent command; partial transcripts
  never execute changes. Preserve command IDs during reconnect/retry.
- Distinguish stopping playback from cancelling a task. A completed external
  action cannot be undone just because the user interrupts speech.
- Stream confirmed task progress to the voice layer. Spoken success must come
  from task results, not predicted completion.
- Keep customer-facing Agent voice testing separate from speaking with the
  builder. The former can reuse the existing Dograh voice tester.

Source: https://developers.openai.com/api/docs/guides/realtime

## Production work remaining after prototype review

1. Reuse `RenderWorkflow.tsx`, `EditorSetup.tsx`, and the existing graph renderer
   for the real workbench. Replace the mock dispatcher with generated API client
   calls and a recoverable server event stream.
2. Add persistent workbench sessions, events, jobs, suite versions, evaluation
   runs and case results. Freeze candidates including graph, variables, and
   model configuration. Bind each run to its exact snapshot.
3. Extract the existing MCP TypeScript parser/validation/write logic into
   shared services. Existing `create_workflow` publishes v1, and existing draft
   saves mutate in place; neither is suitable as an immutable candidate writer.
4. Connect a pinned Codex runtime via the direct-tools adapter. Handle start,
   resume, steer, cancellation, tool errors, retry idempotency, and tenant scope.
   Persist Agent state in Dograh, not exclusively in Codex thread history.
5. Add an evaluation-only candidate resolver to `run_creation.py` and use the
   existing text-chat runtime for actual turns. Use test tool responses,
   deterministic assertions, and a separate judge. Do not let the optimizer
   change the frozen evaluation criteria while improving a candidate.
6. Add real voice collaboration with tenant-authorized ephemeral sessions,
   transcripts, barge-in, reconnect, and confirmed result narration.
7. Switch `/workflow/create` from the MPS generation endpoint to workbench
   sessions after end-to-end validation. Retain unaffected MPS functions.

Production acceptance must include cross-organization access tests, stale
revision rejection, candidate reproducibility, cancelled-task non-adoption,
duplicate-command handling, and microphone/session cleanup. The UI-only checks
for this prototype are not evidence that those backend properties exist.

## Attachment implementation for the real MVP (proposed, not implemented)

Reuse the existing authenticated file/storage services after auditing their
organization scoping. Keep builder attachments distinct from the Agent's runtime
knowledge base: a build reference should not become customer-facing retrieval
content automatically.

- Add `workbench_assets`: organization ID, uploader ID, session ID, private object
  key, original filename, verified media type, byte size, content digest, status,
  parser version, extraction artifact key, error code, created/deleted timestamps.
- Add `workbench_message_assets`: message ID, asset ID, purpose (construction,
  testing, tuning), immutable asset digest/extraction version. Scope both ends
  to the same organization and session. Jobs freeze exact asset references.
- Provide authenticated upload initiation/completion, metadata/preview, retry and
  delete endpoints. The server verifies size/type, object ownership and digest;
  do not trust client filenames or MIME headers. Process files asynchronously.
- Show uploading, queued, parsing, ready and failed states. Only ready assets may
  enter a task; failed assets can be retried or removed. This UI currently shows
  local-file selection, not a fabricated successful parse/upload.
- Expose `list_session_assets` and `read_asset_excerpt` as bounded internal Codex
  tools over the same asset service used by the MCP adapter. Return page/section
  citations. File content is task data, not authority to execute new tools or
  override user instructions. Do not pass private storage keys to browser clients.
- Text and voice commands carry explicit asset IDs in the same session. A voice
  utterance commits the selected attachment set once, with the command's
  idempotency key; reconnecting cannot silently add a different set of assets.
- PDF/DOCX/text extraction and CSV/XLSX table reading belong to the MVP. Image
  understanding/OCR requires a separate configured parser/model path. Keep
  unreadable/encrypted/scanned/unsupported files visible with actionable errors.
- Acceptance: cross-tenant asset/message binding, size/type validation, failed
  parsing, removed assets, duplicate commands, immutable file versions and
  voice/text reference equivalence. UI role previews do not prove isolation.

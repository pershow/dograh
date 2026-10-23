"""Per-job, loopback Responses adapter for Dograh OpenAI-compatible LLM connections.

Codex speaks Responses. Older Qwen models expose Chat Completions only. This
adapter translates protocol data; Codex still owns planning and tool execution.
Only the supplied Dograh function allowlist reaches the provider or returns to
Codex. No provider credentials enter the Codex process or its filesystem.
"""

import asyncio
import contextlib
import json
import secrets
import time
import uuid

import httpx
from aiohttp import web

from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.streaming import (
    ChatStream,
    sse_data,
    validate_response_item,
    validate_tool_arguments,
)

# Preserve the authenticated app-server's identity and turn metadata. Never
# forward arbitrary headers: the loopback token and provider credentials belong
# to separate trust boundaries. Identity headers come from the Codex runtime's
# initialize handshake; this relay does not synthesize or rewrite them.
CODEX_REQUEST_HEADERS = (
    "user-agent",
    "originator",
    "session-id",
    "thread-id",
    "x-client-request-id",
    "x-codex-beta-features",
    "x-codex-turn-metadata",
    "x-codex-turn-state",
    "x-codex-window-id",
)
RESPONSES_OPTIONS = (
    "reasoning",
    "include",
    "text",
    "prompt_cache_key",
    "client_metadata",
    "parallel_tool_calls",
)
CODEX_CLIENT_RESTRICTION = "This account only allows Codex official clients"


async def provider_http_failure(upstream, protocol):
    status = upstream.status_code
    if status == 403:
        # Some gateways append SSE to their JSON error. Read only a bounded
        # prefix and decode the first JSON value; never echo arbitrary provider
        # bodies (which can contain credentials or request content) to the UI.
        prefix = bytearray()
        try:
            async with asyncio.timeout(2):
                async for chunk in upstream.aiter_bytes():
                    prefix.extend(chunk[: 8192 - len(prefix)])
                    try:
                        data, _ = json.JSONDecoder().raw_decode(
                            prefix.decode("utf-8").lstrip()
                        )
                    except (ValueError, UnicodeDecodeError):
                        if len(prefix) >= 8192:
                            break
                        continue
                    error = data.get("error") if isinstance(data, dict) else None
                    if (
                        isinstance(error, dict)
                        and error.get("message") == CODEX_CLIENT_RESTRICTION
                    ):
                        return ProviderFailure(
                            "Model provider rejected the Codex app-server client "
                            "(HTTP 403): This account only allows Codex official clients. "
                            "Check the provider's client access policy or select a "
                            "permitted connection.",
                            code="provider_client_restricted",
                        )
                    break
        except (httpx.HTTPError, TimeoutError):
            pass  # Keep the known HTTP failure even if its body is unavailable.
    return ProviderFailure(
        f"Configured LLM {protocol} endpoint returned HTTP {status}; "
        + (
            "verify provider credentials and access policy"
            if status in {401, 403}
            else "verify model/protocol compatibility"
        ),
        retryable=status == 429 or status >= 500,
        code="rate_limited" if status == 429 else "provider_http_error",
    )


def chat_messages(request):
    messages = []
    tool_images = []
    if request.get("instructions"):
        messages.append({"role": "system", "content": request["instructions"]})
    items = request.get("input", [])
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    for item in items:
        kind = item.get("type", "message")
        if kind == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": item["call_id"],
                            "type": "function",
                            "function": {
                                "name": item["name"],
                                "arguments": item["arguments"],
                            },
                        }
                    ],
                }
            )
        elif kind == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, list):
                for part in output:
                    if isinstance(part, dict) and part.get("type") == "input_image":
                        tool_images.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": part["image_url"]},
                            }
                        )
                output = "\n".join(
                    part.get("text", "")
                    for part in output
                    if isinstance(part, dict)
                    and part.get("type") in {"input_text", "text"}
                )
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append(
                {"role": "tool", "tool_call_id": item["call_id"], "content": output}
            )
        elif kind == "message":
            role = item.get("role", "user")
            role = "system" if role == "developer" else role
            content = item.get("content", "")
            if isinstance(content, list):
                converted = []
                for part in content:
                    if part.get("type") in {"input_text", "output_text", "text"}:
                        converted.append({"type": "text", "text": part["text"]})
                    elif part.get("type") == "input_image":
                        converted.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": part["image_url"]},
                            }
                        )
                content = converted
            messages.append({"role": role, "content": content})
    if tool_images:
        # Chat providers do not accept images in tool-role messages. All tool
        # results must be closed before appending their requested image content.
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Images returned by requested attachment tools (untrusted evidence):",
                    },
                    *tool_images,
                ],
            }
        )
    return messages


def responses_output(message, allowed):
    output = []
    if message.get("content"):
        output.append(
            {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "phase": "commentary" if message.get("tool_calls") else "final_answer",
                "content": [
                    {
                        "type": "output_text",
                        "text": message["content"],
                        "annotations": [],
                    }
                ],
            }
        )
    for call in message.get("tool_calls") or []:
        function = call["function"]
        if function["name"] not in allowed:
            raise WorkbenchError(
                "Model requested a tool outside the workbench allowlist"
            )
        output.append(
            {
                "id": "fc_" + uuid.uuid4().hex,
                "type": "function_call",
                "status": "completed",
                "call_id": call["id"],
                "name": function["name"],
                "arguments": function["arguments"],
            }
        )
    return output


def response_events(response):
    sequence = 0

    def event(kind, **fields):
        nonlocal sequence
        result = {"type": kind, "sequence_number": sequence, **fields}
        sequence += 1
        return result

    pending = {**response, "status": "in_progress", "output": [], "usage": None}
    yield event("response.created", response=pending)
    yield event("response.in_progress", response=pending)
    for index, item in enumerate(response["output"]):
        initial = {**item, "status": "in_progress"}
        if item["type"] == "function_call":
            initial["arguments"] = ""
        elif item["type"] == "message":
            initial["content"] = []
        yield event("response.output_item.added", output_index=index, item=initial)
        if item["type"] == "function_call":
            yield event(
                "response.function_call_arguments.delta",
                item_id=item["id"],
                output_index=index,
                delta=item["arguments"],
            )
            yield event(
                "response.function_call_arguments.done",
                item_id=item["id"],
                output_index=index,
                arguments=item["arguments"],
            )
        elif item["type"] == "message":
            for content_index, part in enumerate(item["content"]):
                if part.get("type") != "output_text":
                    continue
                yield event(
                    "response.content_part.added",
                    item_id=item["id"],
                    output_index=index,
                    content_index=content_index,
                    part={**part, "text": ""},
                )
                yield event(
                    "response.output_text.delta",
                    item_id=item["id"],
                    output_index=index,
                    content_index=content_index,
                    delta=part["text"],
                )
                yield event(
                    "response.output_text.done",
                    item_id=item["id"],
                    output_index=index,
                    content_index=content_index,
                    text=part["text"],
                )
                yield event(
                    "response.content_part.done",
                    item_id=item["id"],
                    output_index=index,
                    content_index=content_index,
                    part=part,
                )
        yield event("response.output_item.done", output_index=index, item=item)
    yield event("response.completed", response=response)


class ProviderFailure(WorkbenchError):
    def __init__(self, message, *, retryable=False, code="model_error"):
        super().__init__(message)
        self.retryable, self.code = retryable, code


class ModelGateway:
    def __init__(
        self,
        llm_config,
        allowed_tools,
        *,
        protocol="chat",
        max_requests=None,
        max_tokens=None,
        max_output_tokens=32768,
        idle_timeout=90,
        request_timeout=None,
        max_retries=2,
        on_event=None,
        progress_interval=5,
        tool_specs=None,
    ):
        if protocol not in {"chat", "responses"}:
            raise WorkbenchError("Unknown model protocol")
        self.protocol = protocol
        self.config, self.allowed = llm_config, set(allowed_tools)
        self.tool_specs = tool_specs
        self.token = secrets.token_urlsafe(32)
        self.request_count, self.tokens = 0, 0
        self.max_requests = max_requests if max_requests and max_requests > 0 else None
        self.max_tokens = max_tokens if max_tokens and max_tokens > 0 else None
        self.max_output_tokens = max_output_tokens
        self.idle_timeout, self.request_timeout = idle_timeout, request_timeout
        self.max_retries, self.on_event = max_retries, on_event
        self.progress_interval = progress_interval
        self.next_required_tool = None
        self.budget_exceeded = False
        self.runner = None
        self.last_error = None
        self.last_error_code = None
        self.invalid_tool_name = None
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(idle_timeout, connect=15))

    async def __aenter__(self):
        from api.utils.url_security import validate_user_configured_service_url

        base = getattr(self.config, "base_url", None) or "https://api.openai.com/v1"
        validate_user_configured_service_url(base, field_name="base_url")
        self.base = base.rstrip("/")
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_post("/v1/responses", self.handle)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"
        return self

    async def __aexit__(self, *_):
        if self.runner:
            await self.runner.cleanup()
        await self.client.aclose()

    def check_budget(self):
        request_limit = (
            self.max_requests is not None and self.request_count >= self.max_requests
        )
        token_limit = self.max_tokens is not None and self.tokens >= self.max_tokens
        if not request_limit and not token_limit:
            return
        self.budget_exceeded = True
        limit = "request" if request_limit else "token"
        raise ProviderFailure(
            f"Workbench model {limit} budget exceeded: "
            f"{self.request_count}/{self.max_requests or 'unlimited'} requests, "
            f"{self.tokens}/{self.max_tokens or 'unlimited'} cumulative tokens. "
            "Reduce task context or adjust the server WORKBENCH_MAX_MODEL_"
            + ("REQUESTS" if limit == "request" else "TOKENS")
            + " setting.",
            code="workbench_budget_exceeded",
        )

    def payload(self, body):
        allowed = [
            t
            for t in body.get("tools", [])
            if t.get("type") == "function" and t.get("name") in self.allowed
        ]
        if self.tool_specs is not None:
            # Durable threads retain a stable catalog, but the model must see
            # this turn's permissions and schemas (e.g. no full-code rewrite).
            current = {s["name"]: s for s in self.tool_specs()}
            allowed = [
                {
                    **t,
                    "description": current[t["name"]]["description"],
                    "parameters": current[t["name"]]["inputSchema"],
                }
                for t in allowed
                if t["name"] in current
            ]
        output_limit = self.max_output_tokens
        if self.max_tokens is not None:
            output_limit = min(output_limit, self.max_tokens - self.tokens)
        if self.protocol == "responses":
            payload = {
                "model": self.config.model,
                "input": body.get("input", []),
                "tools": allowed,
                "stream": True,
                "store": False,
                "max_output_tokens": output_limit,
            }
            if body.get("instructions"):
                payload["instructions"] = body["instructions"]
            for name in RESPONSES_OPTIONS:
                if name in body:
                    payload[name] = body[name]
        else:
            payload = {
                "model": self.config.model,
                "messages": chat_messages(body),
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": output_limit,
            }
            if allowed:
                payload["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            k: t[k]
                            for k in ("name", "description", "parameters")
                            if k in t
                        },
                    }
                    for t in allowed
                ]
                payload["parallel_tool_calls"] = False
            if "dashscope" in self.base or "maas.aliyuncs.com" in self.base:
                payload["enable_thinking"] = False
        required = self.next_required_tool
        if required:
            if required not in {t["name"] for t in allowed}:
                raise WorkbenchError("Required completion tool is unavailable")
            payload["tool_choice"] = (
                {"type": "function", "name": required}
                if self.protocol == "responses"
                else {"type": "function", "function": {"name": required}}
            )
            self.next_required_tool = None
        return payload, output_limit

    async def upstream_events(self, payload, result, output_limit, client_headers):
        endpoint = "/responses" if self.protocol == "responses" else "/chat/completions"
        async with self.client.stream(
            "POST",
            self.base + endpoint,
            headers={
                **client_headers,
                "Authorization": "Bearer " + self.config.api_key,
            },
            json=payload,
        ) as upstream:
            if upstream.status_code != 200:
                raise await provider_http_failure(upstream, self.protocol)
            if "text/event-stream" not in upstream.headers.get("content-type", ""):
                # Some compatible providers ignore stream=true. Still bound their read.
                data = json.loads(await upstream.aread())
                result["usage"] = data.get("usage") or {}
                if self.protocol == "chat":
                    choice = data["choices"][0]
                    adapter = ChatStream(self.allowed, output_limit)
                    adapter.finish_reason = choice.get("finish_reason")
                    adapter.finish()  # Reject truncation before completing any tool.
                    result["output"] = responses_output(choice["message"], self.allowed)
                    for item in result["output"]:
                        if item["type"] == "function_call":
                            validate_tool_arguments(item)
                else:
                    self.validate_completed(data, output_limit)
                    result["output"] = data.get("output", [])
                for event in response_events({**result, "status": "completed"}):
                    if event["type"] not in {
                        "response.created",
                        "response.in_progress",
                        "response.completed",
                    }:
                        yield event
                return
            if self.protocol == "chat":
                adapter = ChatStream(self.allowed, output_limit)
                try:
                    async for chunk in sse_data(upstream):
                        reasoning = sum(
                            len(
                                str(
                                    (c.get("delta") or {}).get("reasoning_content")
                                    or ""
                                )
                            )
                            for c in chunk.get("choices", [])
                        )
                        if reasoning:
                            # Record activity without exposing provider reasoning text.
                            yield {
                                "type": "dograh.reasoning_activity",
                                "characters": reasoning,
                            }
                        for event in adapter.feed(chunk):
                            yield event
                    final = adapter.finish()
                    result["output"] = adapter.items
                    for event in final:
                        yield event
                finally:
                    result["usage"] = adapter.usage
            else:
                completed = False
                pending = []
                async for event in sse_data(upstream):
                    kind = event.get("type", "")
                    if "item" in event:
                        validate_response_item(event["item"], self.allowed)
                    if kind in {"response.failed", "response.incomplete", "error"}:
                        data = event.get("response") or {}
                        result["usage"] = data.get("usage") or {}
                        self.validate_completed(data, output_limit)
                        raise WorkbenchError(
                            "Model provider reported a streaming error"
                        )
                    if kind == "response.completed":
                        data = event["response"]
                        result["usage"] = data.get("usage") or {}
                        self.validate_completed(data, output_limit)
                        result["output"] = data.get("output", [])
                        completed = True
                        break
                    if kind in {
                        "response.output_item.done",
                        "response.function_call_arguments.done",
                        "response.output_text.done",
                        "response.content_part.done",
                    }:
                        pending.append(event)
                    elif kind.startswith(
                        (
                            "response.output_",
                            "response.content_part.",
                            "response.function_call_arguments.",
                            "response.reasoning",
                        )
                    ):
                        yield event
                if not completed:
                    raise WorkbenchError(
                        "Model stream ended without a complete response; no incomplete tool was executed"
                    )
                for event in pending:
                    yield event

    async def progressing_events(self, payload, result, output_limit, client_headers):
        # Network keepalives and empty SSE events are not model progress. Keep
        # extending while content/tools/reasoning arrive, with no total deadline.
        async with contextlib.aclosing(
            self.upstream_events(payload, result, output_limit, client_headers)
        ) as iterator:
            while True:
                try:
                    event = await asyncio.wait_for(anext(iterator), self.idle_timeout)
                except StopAsyncIteration:
                    return
                except TimeoutError as error:
                    raise httpx.ReadTimeout("No meaningful model progress") from error
                yield event

    def validate_completed(self, data, output_limit):
        if data.get("status") != "completed":
            reason = (data.get("incomplete_details") or {}).get("reason")
            if reason == "max_output_tokens":
                raise WorkbenchError(
                    f"Model output reached the {output_limit} token limit; no incomplete tool was executed. "
                    "Increase WORKBENCH_MAX_OUTPUT_TOKENS or split the build into smaller changes."
                )
            raise WorkbenchError(
                "Responses request did not complete; no incomplete tool was executed"
                + (
                    f" (provider code: {str((data.get('error') or {}).get('code', 'unknown'))[:100]})"
                    if isinstance(data.get("error"), dict)
                    else ""
                )
            )
        for item in data.get("output", []):
            validate_response_item(item, self.allowed)
            if item.get("type") == "function_call":
                validate_tool_arguments(item)

    async def handle(self, request):
        if not secrets.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + self.token
        ):
            raise web.HTTPUnauthorized()
        client_headers = (
            {
                name: request.headers[name]
                for name in CODEX_REQUEST_HEADERS
                if name in request.headers
            }
            if self.protocol == "responses"
            else {}
        )
        try:
            self.check_budget()
            body = await request.json()
            payload, output_limit = self.payload(body)
        except WorkbenchError as error:
            self.last_error = str(error)
            self.last_error_code = getattr(error, "code", "model_error")
            return web.json_response(
                {
                    "error": {
                        "code": getattr(error, "code", "model_error"),
                        "message": self.last_error,
                    }
                },
                status=400,
            )
        started = time.monotonic()
        progress = {
            "phase": "waiting",
            "attempt": 1,
            "max_attempts": self.max_retries + 1,
            "output_characters": 0,
            "max_output_tokens": output_limit,
        }
        stream = None
        sequence = 0
        lock = asyncio.Lock()
        result = {
            "id": "resp_" + uuid.uuid4().hex,
            "object": "response",
            "created_at": int(time.time()),
            "status": "in_progress",
            "model": self.config.model,
            "output": [],
            "usage": {},
        }
        if body.get("stream"):
            stream = web.StreamResponse(
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-store",
                }
            )
            await stream.prepare(request)

        async def write(event):
            nonlocal sequence
            if stream:
                async with lock:
                    event = {**event, "sequence_number": sequence}
                    sequence += 1
                    await stream.write(
                        (
                            "event: "
                            + event["type"]
                            + "\ndata: "
                            + json.dumps(event, ensure_ascii=False)
                            + "\n\n"
                        ).encode()
                    )

        async def notify():
            if self.on_event:
                await self.on_event(
                    "model.progress",
                    {
                        **progress,
                        "elapsed_seconds": round(time.monotonic() - started),
                        "request": self.request_count,
                        "request_id": result["id"],
                    },
                )

        async def heartbeat():
            while True:
                await asyncio.sleep(self.progress_interval)
                await notify()
                if stream:
                    async with lock:
                        await stream.write(b": keepalive\n\n")

        await write({"type": "response.created", "response": result})
        await write({"type": "response.in_progress", "response": result})
        pulse = asyncio.create_task(heartbeat())
        exposed = False
        failure = None
        try:
            async with asyncio.timeout(self.request_timeout):
                for attempt in range(1, self.max_retries + 2):
                    self.check_budget()
                    self.request_count += 1
                    progress.update(phase="waiting", attempt=attempt, reason=None)
                    await notify()
                    result["usage"] = {}
                    try:
                        async for event in self.progressing_events(
                            payload, result, output_limit, client_headers
                        ):
                            if event["type"] == "dograh.reasoning_activity":
                                progress["phase"] = "generating"
                                progress["output_characters"] += event["characters"]
                                continue
                            # Don't replay a response once any model output has reached Codex.
                            exposed = True
                            progress["phase"] = "generating"
                            progress["output_characters"] += len(event.get("delta", ""))
                            await write(event)
                        break
                    except httpx.ConnectTimeout as error:
                        failure = ProviderFailure(
                            "Model connection timed out after 15s.",
                            retryable=True,
                            code="model_timeout",
                        )
                        failure.__cause__ = error
                    except httpx.TimeoutException as error:
                        failure = ProviderFailure(
                            f"Model returned no data for {self.idle_timeout:g}s (I/O timeout).",
                            retryable=True,
                            code="model_timeout",
                        )
                        failure.__cause__ = error
                    except httpx.HTTPError as error:
                        failure = ProviderFailure(
                            "Model connection interrupted.",
                            retryable=True,
                            code="model_connection_error",
                        )
                        failure.__cause__ = error
                    except ProviderFailure as error:
                        failure = error
                    finally:
                        usage = result.get("usage") or {}
                        self.tokens += usage.get(
                            "total_tokens",
                            usage.get("input_tokens", usage.get("prompt_tokens", 0))
                            + usage.get(
                                "output_tokens", usage.get("completion_tokens", 0)
                            ),
                        )
                    if exposed or not failure.retryable or attempt > self.max_retries:
                        raise failure
                    progress.update(
                        phase="retrying", reason=failure.code, next_attempt=attempt + 1
                    )
                    await notify()
                    await asyncio.sleep(min(attempt, 2))
                usage = result.get("usage") or {}
                result.update(
                    status="completed",
                    error=None,
                    usage={
                        "input_tokens": usage.get(
                            "input_tokens", usage.get("prompt_tokens", 0)
                        ),
                        "output_tokens": usage.get(
                            "output_tokens", usage.get("completion_tokens", 0)
                        ),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                )
                progress["phase"] = "completed"
                self.last_error = None
                self.last_error_code = None
                self.invalid_tool_name = None
                await notify()
                # Save the completion receipt before the client closes a finished
                # stream, otherwise the UI can retain a stale waiting status.
                await write({"type": "response.completed", "response": result})
        except (WorkbenchError, KeyError, ValueError, TimeoutError) as error:
            if isinstance(error, TimeoutError):
                message = f"Model request exceeded {self.request_timeout:g}s total time budget."
                code = "model_deadline"
            else:
                message = (
                    str(error)
                    if isinstance(error, WorkbenchError)
                    else "Model returned an invalid streaming response"
                )
                code = getattr(error, "code", "model_error")
            self.last_error = message
            self.last_error_code = code
            self.invalid_tool_name = getattr(error, "tool_name", None)
            progress.update(phase="failed", reason=code, message=message)
            await notify()
            result.update(
                status="failed", error={"code": code, "message": message}, output=[]
            )
            await write({"type": "response.failed", "response": result})
        finally:
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pulse
        if stream:
            await stream.write_eof()
            return stream
        if result["status"] == "failed":
            return web.json_response({"error": result["error"]}, status=502)
        return web.json_response(result)

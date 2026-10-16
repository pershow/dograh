"""Per-job, loopback Responses adapter for Dograh OpenAI-compatible LLM connections.

Codex speaks Responses. Older Qwen models expose Chat Completions only. This
adapter translates protocol data; Codex still owns planning and tool execution.
Only the supplied Dograh function allowlist reaches the provider or returns to
Codex. No provider credentials enter the Codex process or its filesystem.
"""

import json
import secrets
import time
import uuid

import httpx
from aiohttp import web

from api.services.agent_workbench.errors import WorkbenchError


def chat_messages(request):
    messages = []
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


class ModelGateway:
    def __init__(self, llm_config, allowed_tools, *, protocol="chat"):
        if protocol not in {"chat", "responses"}:
            raise WorkbenchError("Unknown model protocol")
        self.protocol = protocol
        self.config, self.allowed = llm_config, set(allowed_tools)
        self.token = secrets.token_urlsafe(32)
        self.request_count, self.tokens = 0, 0
        self.runner = None
        self.last_error = None
        self.client = httpx.AsyncClient(timeout=90)

    async def __aenter__(self):
        from api.utils.url_security import validate_user_configured_service_url

        base = getattr(self.config, "base_url", None) or "https://api.openai.com/v1"
        # Use the same provider URL policy as the normal runtime.
        validate_user_configured_service_url(base, field_name="base_url")
        self.base = base.rstrip("/")
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_post("/v1/responses", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"
        return self

    async def __aexit__(self, *_):
        if self.runner:
            await self.runner.cleanup()
        await self.client.aclose()

    async def handle(self, request):
        if not secrets.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + self.token
        ):
            raise web.HTTPUnauthorized()
        self.request_count += 1
        if self.request_count > 40 or self.tokens > 150000:
            self.last_error = "Workbench model budget exceeded"
            return web.json_response(
                {"error": {"message": "Workbench model budget exceeded"}}, status=429
            )
        try:
            body = await request.json()
            allowed = [
                t
                for t in body.get("tools", [])
                if t.get("type") == "function" and t.get("name") in self.allowed
            ]
            if self.protocol == "responses":
                payload = {
                    "model": self.config.model,
                    "input": body.get("input", []),
                    "tools": allowed,
                    "stream": False,
                    "store": False,
                    "max_output_tokens": 8192,
                }
                if body.get("instructions"):
                    payload["instructions"] = body["instructions"]
                endpoint = "/responses"
            else:
                functions = [
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
                payload = {
                    "model": self.config.model,
                    "messages": chat_messages(body),
                    "stream": False,
                    "max_tokens": 8192,
                }
                if functions:
                    payload["tools"] = functions
                    payload["parallel_tool_calls"] = False
                if "dashscope" in self.base or "maas.aliyuncs.com" in self.base:
                    payload["enable_thinking"] = False
                endpoint = "/chat/completions"
            upstream = await self.client.post(
                self.base + endpoint,
                headers={"Authorization": "Bearer " + self.config.api_key},
                json=payload,
            )
            if upstream.status_code != 200:
                self.last_error = f"Configured LLM {self.protocol} endpoint returned HTTP {upstream.status_code}; verify model/protocol compatibility"
                return web.json_response(
                    {
                        "error": {
                            "message": f"Configured LLM {self.protocol} endpoint returned HTTP {upstream.status_code}; verify model/protocol compatibility"
                        }
                    },
                    status=502,
                )
            result = upstream.json()
            usage = result.get("usage") or {}
            if self.protocol == "responses":
                if result.get("status") != "completed":
                    raise WorkbenchError(
                        "Responses request did not complete: "
                        + str(result.get("status"))
                    )
                output = result.get("output", [])
                for item in output:
                    if (
                        item.get("type") == "function_call"
                        and item.get("name") not in self.allowed
                    ):
                        raise WorkbenchError(
                            "Model requested a tool outside the workbench allowlist"
                        )
                    if item.get("type") not in {
                        "message",
                        "function_call",
                        "reasoning",
                    }:
                        raise WorkbenchError(
                            "Responses returned an unsupported hosted tool item"
                        )
            else:
                choice = result["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise WorkbenchError(
                        "Model output exceeded token limit; simplify the requested change"
                    )
                output = responses_output(choice["message"], self.allowed)
            self.tokens += usage.get("total_tokens", 0)
            response = {
                "id": "resp_" + uuid.uuid4().hex,
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": self.config.model,
                "output": output,
                "error": None,
                "usage": {
                    "input_tokens": usage.get(
                        "input_tokens", usage.get("prompt_tokens", 0)
                    ),
                    "output_tokens": usage.get(
                        "output_tokens", usage.get("completion_tokens", 0)
                    ),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
            if not body.get("stream"):
                return web.json_response(response)
            stream = web.StreamResponse(
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-store",
                }
            )
            await stream.prepare(request)
            for event in response_events(response):
                await stream.write(
                    (
                        "event: "
                        + event["type"]
                        + "\ndata: "
                        + json.dumps(event, ensure_ascii=False)
                        + "\n\n"
                    ).encode()
                )
            await stream.write_eof()
            return stream
        except (WorkbenchError, KeyError, ValueError, httpx.HTTPError) as error:
            message = (
                str(error)
                if isinstance(error, WorkbenchError)
                else "Configured LLM response could not be processed"
            )
            self.last_error = message
            return web.json_response({"error": {"message": message}}, status=502)

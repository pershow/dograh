"""Incremental Chat Completions -> Responses events, with atomic tool completion."""

import json
import uuid

from api.services.agent_workbench.errors import WorkbenchError


class InvalidToolArguments(WorkbenchError):
    code = "model_invalid_tool_arguments"

    def __init__(self, name, detail):
        super().__init__(
            f"Model tool arguments for {name} are invalid JSON ({detail}); no tool in this response was executed."
        )
        self.tool_name = name


def validate_tool_arguments(item):
    try:
        value = json.loads(item.get("arguments", ""))
    except (ValueError, TypeError) as error:
        detail = (
            f"line {error.lineno}, column {error.colno}: {error.msg}"
            if isinstance(error, json.JSONDecodeError)
            else "expected a JSON string"
        )
        raise InvalidToolArguments(item["name"], detail) from error
    if not isinstance(value, dict):
        raise InvalidToolArguments(item["name"], "expected an object")


def decode_sse(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError()
        return value
    except (ValueError, TypeError) as error:
        raise WorkbenchError(
            "Model stream contained an invalid JSON event; no incomplete tool was executed"
        ) from error


async def sse_data(response, *, lines=None):
    """Parse SSE across arbitrary HTTP chunks, including multiline data/comments."""
    data = []
    size = 0
    async for line in lines if lines is not None else response.aiter_lines():
        if line.startswith("data:"):
            value = line[5:].lstrip(" ")
            size += len(value)
            if size > 4 * 1024 * 1024:
                raise WorkbenchError("Model stream event exceeded the size limit")
            data.append(value)
        elif not line and data:
            raw = "\n".join(data)
            data, size = [], 0
            if raw == "[DONE]":
                return
            yield decode_sse(raw)
    if data:
        raw = "\n".join(data)
        if raw != "[DONE]":
            yield decode_sse(raw)


class ChatStream:
    def __init__(self, allowed, output_limit):
        self.allowed = allowed
        self.output_limit = output_limit
        self.items = []
        self.message = None
        self.calls = {}
        self.finish_reason = None
        self.usage = {}

    def feed(self, chunk):
        if chunk.get("error"):
            raise WorkbenchError("Model provider reported a streaming error")
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        events = []
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content") or ""
            if text:
                if self.message is None:
                    self.message = {
                        "id": "msg_" + uuid.uuid4().hex,
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "phase": "commentary",
                        "content": [
                            {"type": "output_text", "text": "", "annotations": []}
                        ],
                    }
                    self.items.append(self.message)
                    events.extend(
                        [
                            {
                                "type": "response.output_item.added",
                                "output_index": len(self.items) - 1,
                                "item": {**self.message, "content": []},
                            },
                            {
                                "type": "response.content_part.added",
                                "output_index": len(self.items) - 1,
                                "item_id": self.message["id"],
                                "content_index": 0,
                                "part": {
                                    "type": "output_text",
                                    "text": "",
                                    "annotations": [],
                                },
                            },
                        ]
                    )
                self.message["content"][0]["text"] += text
                events.append(
                    {
                        "type": "response.output_text.delta",
                        "output_index": self.items.index(self.message),
                        "item_id": self.message["id"],
                        "content_index": 0,
                        "delta": text,
                    }
                )
            for call in delta.get("tool_calls") or []:
                index = call["index"]
                function = call.get("function") or {}
                if index not in self.calls:
                    self.calls[index] = {
                        "id": "fc_" + uuid.uuid4().hex,
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": "",
                        "name": "",
                        "arguments": "",
                    }
                item = self.calls[index]
                if call.get("id"):
                    item["call_id"] = call["id"]
                if function.get("name"):
                    item["name"] += function["name"]
                # Providers send the tool name/id in the first chunk, arguments later.
                # Buffer until the identity is known; never expose an unapproved tool.
                if item["call_id"] and item["name"] and item not in self.items:
                    if item["name"] not in self.allowed:
                        raise WorkbenchError(
                            "Model requested a tool outside the workbench allowlist"
                        )
                    self.items.append(item)
                    events.append(
                        {
                            "type": "response.output_item.added",
                            "output_index": len(self.items) - 1,
                            "item": {**item, "arguments": ""},
                        }
                    )
                    if item["arguments"]:
                        events.append(
                            {
                                "type": "response.function_call_arguments.delta",
                                "output_index": len(self.items) - 1,
                                "item_id": item["id"],
                                "delta": item["arguments"],
                            }
                        )
                arguments = function.get("arguments") or ""
                item["arguments"] += arguments
                if arguments and item in self.items:
                    events.append(
                        {
                            "type": "response.function_call_arguments.delta",
                            "output_index": self.items.index(item),
                            "item_id": item["id"],
                            "delta": arguments,
                        }
                    )
        return events

    def finish(self):
        if self.finish_reason == "length":
            raise WorkbenchError(
                f"Model output reached the {self.output_limit} token limit; no incomplete tool was executed. "
                "Increase WORKBENCH_MAX_OUTPUT_TOKENS or split the build into smaller changes."
            )
        if self.finish_reason not in {"stop", "tool_calls"}:
            raise WorkbenchError(
                "Model stream ended without a complete response; no incomplete tool was executed"
            )
        for item in self.calls.values():
            if item not in self.items or item["name"] not in self.allowed:
                raise WorkbenchError(
                    "Model stream contained an incomplete tool identity"
                )
            validate_tool_arguments(item)
        events = []
        for index, item in enumerate(self.items):
            item["status"] = "completed"
            if item["type"] == "message":
                item["phase"] = "commentary" if self.calls else "final_answer"
                part = item["content"][0]
                events.extend(
                    [
                        {
                            "type": "response.output_text.done",
                            "output_index": index,
                            "item_id": item["id"],
                            "content_index": 0,
                            "text": part["text"],
                        },
                        {
                            "type": "response.content_part.done",
                            "output_index": index,
                            "item_id": item["id"],
                            "content_index": 0,
                            "part": part,
                        },
                    ]
                )
            else:
                events.append(
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": index,
                        "item_id": item["id"],
                        "arguments": item["arguments"],
                    }
                )
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": item,
                }
            )
        return events


def validate_response_item(item, allowed):
    if item.get("type") not in {"message", "function_call", "reasoning"}:
        raise WorkbenchError("Responses returned an unsupported hosted tool item")
    if item.get("type") == "function_call" and item.get("name") not in allowed:
        raise WorkbenchError("Model requested a tool outside the workbench allowlist")

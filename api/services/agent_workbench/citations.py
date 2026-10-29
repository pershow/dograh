"""Literal evidence selection and restricted repair for source-backed decisions."""

import json
import re
from copy import deepcopy
from typing import ClassVar

import jsonschema

from api.services.agent_workbench.errors import (
    RecoverableValidationError,
    WorkbenchError,
)
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.review import ReviewSourceReader
from api.services.agent_workbench.simulation import original_quote

GUIDANCE = """
Select citations using ONLY span_id values from the supplied citation_spans.
The server binds each selection to its immutable original source and literal text.
Do not write source IDs or quote text yourself. Read unread source pages as usual.
Select enough spans to support the complete requirement, including its conditions
and exceptions. A valid span is evidence, not proof that your interpretation follows
from it; independently verify relevance, completeness and business meaning.
If citations are rejected, all business fields and valid citations are locked.
Use repair_citations with the returned draft_id and only the rejected positions.
Never remove a requirement, change its severity, or rewrite the complete submission
to bypass an error. Fix all reported positions together when possible.
"""


class CitationErrors(RecoverableValidationError):
    def __init__(self, issues, *, draft_id=None, progressed=False):
        self.issues = issues
        self.draft_id = draft_id
        self.progressed = progressed
        super().__init__(
            "引用校验失败："
            + "；".join(f"{x['path']}: {x['reason']}" for x in issues[:8])
            + (f"；共 {len(issues)} 项错误。" if len(issues) > 8 else "")
        )

    def feedback(self):
        return {
            "error": str(self),
            "issues": self.issues,
            "draft_id": self.draft_id,
            "repair_tool": "repair_citations" if self.draft_id else None,
        }

    def diagnostic(self):
        # Persist bounded evidence even when the third rejection ends the turn.
        return {
            **self.feedback(),
            "issues": self.issues[:20],
            "issue_count": len(self.issues),
        }


def validate_literal_citations(value, group, records):
    """Collect every invalid quote before changing anything; no fuzzy acceptance."""
    issues, resolved = [], []
    for i, item in enumerate(value[group]):
        for j, citation in enumerate(item["citations"]):
            source = records.get(citation["id"], {})
            literal = original_quote(citation["quote"], source.get("text", ""))
            reason = None
            if (
                source.get("source") != "basis"
                or source.get("source_kind") == "reference"
            ):
                reason = "必须引用独立的原始业务资料"
            elif not citation["quote"].strip() or literal is None:
                reason = "引用未逐字匹配原文；不能改写、拼接或删除业务要求"
            if reason:
                issues.append(
                    {
                        "path": f"{group}[{i}].citations[{j}]",
                        "reason": reason,
                        "source_id": citation["id"][:200],
                        "quote": citation["quote"][:240],
                    }
                )
            else:
                resolved.append((citation, literal))
    if issues:
        raise CitationErrors(issues)
    for citation, literal in resolved:
        citation["quote"] = literal


class CitationSourceReader(ReviewSourceReader):
    def __init__(self, records):
        self.spans = {}
        annotated = deepcopy(records)
        for record in annotated:
            spans = []
            if record["source"] == "basis" and record.get("source_kind") != "reference":
                text = record["text"]
                # Every nonblank character remains selectable. Sentence/line
                # boundaries are conveniences, never semantic rewrites.
                # Coalesce very short clauses so annotation overhead stays
                # bounded for long lists without losing source characters.
                target_size = max(1, (len(text) + 63) // 64)
                group_start = 0
                for match in re.finditer(
                    r".+?(?:[。！？；;\n]+[ \t\r\n]*|$)", text, re.DOTALL
                ):
                    if match.end() - group_start < target_size and match.end() < len(
                        text
                    ):
                        continue
                    for start in range(group_start, match.end(), 600):
                        end = min(start + 600, match.end())
                        quote = text[start:end]
                        if not quote.strip():
                            continue
                        key = (
                            "cite_" + fingerprint([record["id"], text, start, end])[:20]
                        )
                        self.spans[key] = {"id": record["id"], "quote": quote}
                        spans.append(
                            {"span_id": key, "start": start, "end": end, "text": quote}
                        )
                    group_start = match.end()
            record["citation_spans"] = spans
        super().__init__({"records": annotated})

    def encode(self, value, group):
        """Use the same selections when handing a draft to an independent verifier."""
        reverse = {(c["id"], c["quote"]): key for key, c in self.spans.items()}
        result = deepcopy(value)
        for item in result[group]:
            item["citations"] = [
                {"span_id": reverse[(c["id"], c["quote"])]} for c in item["citations"]
            ]
        return result


class CitationSubmission:
    """Keep business content and accepted evidence immutable during quote repair."""

    tool_name = "repair_citations"
    selection_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"span_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        "required": ["span_id"],
    }
    repair_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "draft_id": {"type": "string"},
            "patches": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "item_index": {"type": "integer", "minimum": 0},
                        "citation_index": {"type": "integer", "minimum": 0},
                        "span_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    },
                    "required": ["item_index", "citation_index", "span_id"],
                },
            },
        },
        "required": ["draft_id", "patches"],
    }

    def __init__(self, schema, group, reader, validate):
        self.schema = deepcopy(schema)
        self.schema["properties"][group]["items"]["properties"]["citations"][
            "items"
        ] = self.selection_schema
        self.group, self.reader, self.validate = group, reader, validate
        self.draft = None
        self.errors = {}

    @property
    def pending(self):
        return self.draft is not None

    @property
    def draft_id(self):
        return fingerprint(self.draft)

    def spec(self):
        return {
            "name": self.tool_name,
            "description": "Repair only rejected citation selections; all other fields are locked.",
            "inputSchema": self.repair_schema,
        }

    def _check(self, value):
        errors = {}
        result = deepcopy(value)
        for i, item in enumerate(result[self.group]):
            for j, citation in enumerate(item["citations"]):
                key = citation["span_id"]
                if key not in self.reader.spans:
                    errors[i, j] = {
                        "path": f"{self.group}[{i}].citations[{j}]",
                        "item_index": i,
                        "citation_index": j,
                        "reason": "未知的原文片段；请从本任务 citation_spans 选择",
                        "span_id": key,
                    }
                else:
                    item["citations"][j] = deepcopy(self.reader.spans[key])
        return result, errors

    def __call__(self, arguments):
        if self.pending:
            raise CitationErrors(list(self.errors.values()), draft_id=self.draft_id)
        self.reader.require_read()
        jsonschema.validate(arguments, self.schema)
        resolved, errors = self._check(arguments)
        if errors:
            self.draft, self.errors = deepcopy(arguments), errors
            raise CitationErrors(list(errors.values()), draft_id=self.draft_id)
        self.validate(resolved)
        arguments.clear()
        arguments.update(resolved)

    def repair(self, arguments):
        jsonschema.validate(arguments, self.repair_schema)
        if not self.pending or arguments["draft_id"] != self.draft_id:
            raise WorkbenchError(
                "引用修复版本已失效或不存在；使用最近返回的 draft_id。"
            )
        result = deepcopy(self.draft)
        seen = set()
        for patch in arguments["patches"]:
            position = patch["item_index"], patch["citation_index"]
            if position not in self.errors or position in seen:
                raise WorkbenchError("只能修复尚未通过的引用，不能重复或修改有效字段。")
            seen.add(position)
            result[self.group][position[0]]["citations"][position[1]] = {
                "span_id": patch["span_id"]
            }
        resolved, errors = self._check(result)
        progressed = set(errors) < set(self.errors)
        if errors:
            self.draft, self.errors = result, errors
            raise CitationErrors(
                list(errors.values()), draft_id=self.draft_id, progressed=progressed
            )
        self.reader.require_read()
        self.validate(resolved)
        self.draft, self.errors = None, {}
        return resolved

    def resume_instructions(self):
        if not self.pending:
            return ""
        return "\nContinue the locked draft with repair_citations: " + json.dumps(
            CitationErrors(
                list(self.errors.values()), draft_id=self.draft_id
            ).feedback(),
            ensure_ascii=False,
        )

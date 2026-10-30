"""Source-bound fixture compilation and constrained, checkpointable repairs.

The wire format contains no model-authored scenario IDs, source indices or quotes.
Canonical frozen plans retain their existing format and acceptance checks.
"""

import json
from collections import Counter
from copy import deepcopy

import jsonschema

from api.services.agent_workbench.errors import (
    RecoverableValidationError,
    WorkbenchError,
)
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.simulation import original_quote

GROUPS = ("facts", "goals", "actions")


def obj(properties):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def issue(path, reason, **details):
    return {"path": list(path), "reason": reason, **details}


def schema_issues(value, schema):
    errors = []

    def leaves(error):
        # Nullable corrections must not hide a bad leaf behind a whole-field
        # anyOf error. Descend only when the JSON type selects one branch;
        # ambiguous unions still require repairing the union itself.
        if error.validator in {"anyOf", "oneOf"}:
            branches = error.validator_value
            matching = [
                i
                for i, branch in enumerate(branches)
                if matches_type(error.instance, branch)
            ]
            if len(matching) == 1:
                for child in error.context:
                    if child.schema_path[0] == matching[0]:
                        yield from leaves(child)
                return
        yield error

    for error in (
        leaf
        for parent in jsonschema.Draft202012Validator(schema).iter_errors(value)
        for leaf in leaves(parent)
    ):
        path = list(error.absolute_path)
        if error.validator == "required":
            for key in error.validator_value:
                if key not in error.instance:
                    errors.append(issue([*path, key], "缺少必填字段"))
        elif error.validator == "additionalProperties":
            for key in (
                error.instance.keys() - error.schema.get("properties", {}).keys()
            ):
                errors.append(issue([*path, key], "不允许的额外字段", remove=True))
        else:
            errors.append(
                issue(
                    path,
                    f"字段不满足 {error.validator} 约束",
                    expected=error.validator_value,
                )
            )
    # Required-property errors can repeat at the same parent object.
    return list({json.dumps(e, sort_keys=True): e for e in errors}.values())


def matches_type(value, schema):
    kinds = schema.get("type")
    if kinds is None:
        return True
    if isinstance(kinds, str):
        kinds = [kinds]
    return any(
        jsonschema.Draft202012Validator.TYPE_CHECKER.is_type(value, kind)
        for kind in kinds
    )


def field_schema(schema, path, value):
    """Resolve a schema through nullable objects without changing its constraints."""
    if not path:
        return deepcopy(schema)
    for union in ("anyOf", "oneOf"):
        if union in schema:
            choices = [s for s in schema[union] if matches_type(value, s)]
            if len(choices) != 1:
                raise WorkbenchError("无法唯一确定待修复字段的结构")
            return field_schema(choices[0], path, value)
    key, *tail = path
    child = schema["items"] if isinstance(key, int) else schema["properties"][key]
    current = (
        value[key]
        if isinstance(value, (dict, list)) and (isinstance(key, int) or key in value)
        else None
    )
    return field_schema(child, tail, current)


def plan_issues(value, turns, schema):
    errors = schema_issues(value, schema)
    if errors:
        return errors
    quotes = [t["text"] for t in turns if t["role"] == "user"]
    ids = [x["id"] for group in GROUPS for x in value[group]]
    counts = Counter(ids)
    for group in GROUPS:
        for i, item in enumerate(value[group]):
            if not item["id"].strip() or counts[item["id"]] > 1:
                errors.append(
                    issue([group, i, "id"], "场景标识重复或为空，必须全局唯一")
                )
            if group == "goals" or (
                group == "facts" and item["provenance"] == "customer_quote"
            ):
                if not item["quote"].strip() or not any(
                    original_quote(item["quote"], text) is not None for text in quotes
                ):
                    errors.append(
                        issue(
                            [group, i, "quote"],
                            "引用未匹配客户原话；不能改写、拼接多轮或引用坐席",
                        )
                    )
            elif group == "facts" and not item["reason"].strip():
                errors.append(issue([group, i, "reason"], "测试假设必须说明依据"))
    expected = [i for i, t in enumerate(turns) if t["role"] == "user"]
    actual = Counter(row["turn"] for row in value["source_coverage"])
    if sorted(actual.elements()) != expected:
        errors.append(
            issue(
                ["source_coverage"],
                "转换映射必须覆盖每条客户原话且不能重复",
                missing=sorted(set(expected) - actual.keys()),
                unexpected=sorted(actual.keys() - set(expected)),
                duplicated=[k for k, n in actual.items() if n > 1],
                expected=expected,
            )
        )
    for i, row in enumerate(value["source_coverage"]):
        unknown = set(row["scenario_ids"]) - set(ids)
        if unknown:
            errors.append(
                issue(
                    ["source_coverage", i, "scenario_ids"],
                    "原文映射引用了不存在的场景项目",
                    unknown=sorted(unknown),
                    allowed=ids,
                )
            )
        if row["disposition"] in {"fact", "goal", "action"} and not row["scenario_ids"]:
            errors.append(
                issue(
                    ["source_coverage", i, "scenario_ids"],
                    "实质客户信息不能丢失，必须映射到场景项目",
                )
            )
    return errors + state_issues(value)


def state_issues(value):
    errors = []
    keys = [s["key"] for s in value["initial_state"]]
    for i, key in enumerate(keys):
        if keys.count(key) > 1:
            errors.append(issue(["initial_state", i, "key"], "模拟状态标识重复"))
    for i, action in enumerate(value["actions"]):
        for part in ("preconditions", "effects"):
            names = [s["key"] for s in action[part]]
            for j, name in enumerate(names):
                if names.count(name) > 1 or name not in keys:
                    errors.append(
                        issue(
                            ["actions", i, part, j, "key"],
                            "操作条件和结果必须引用已声明的唯一状态",
                            allowed=keys,
                        )
                    )
    return errors


class FixtureErrors(RecoverableValidationError):
    def __init__(self, issues, *, draft_id=None, progressed=False):
        self.issues, self.draft_id, self.progressed = issues, draft_id, progressed
        super().__init__(
            "场景校验失败："
            + "；".join(f"{x['path']}: {x['reason']}" for x in issues[:8])
            + (f"；共 {len(issues)} 项错误" if len(issues) > 8 else "")
        )

    def feedback(self):
        return {
            "error": str(self),
            "issues": self.issues,
            "draft_id": self.draft_id,
            "repair_tool": "repair_fixture" if self.draft_id else None,
        }

    def diagnostic(self):
        return {
            **self.feedback(),
            "issues": self.issues[:20],
            "issue_count": len(self.issues),
        }


class FixtureCodec:
    """Bind typed selections to the exact source snapshot and canonical plan."""

    def __init__(self, turns):
        from api.services.agent_workbench.test_compilation import PLAN

        self.turns = deepcopy(turns)
        self.schema = deepcopy(PLAN)
        digest = fingerprint(turns)[:12]
        self.sources = {
            f"turn_{i}_{digest}": (i, t["text"])
            for i, t in enumerate(turns)
            if t["role"] == "user"
        }
        selection = {"type": "string", "enum": list(self.sources)}
        for group in GROUPS:
            item = self.schema["properties"][group]["items"]
            item["properties"].pop("id")
            item["required"].remove("id")
            if group != "actions":
                item["properties"].pop("quote")
                item["required"].remove("quote")
                item["properties"]["quote_turn_id"] = (
                    {"anyOf": [selection, {"type": "null"}]}
                    if group == "facts"
                    else selection
                )
                item["required"].append("quote_turn_id")
        row = deepcopy(PLAN["properties"]["source_coverage"]["items"])
        row["properties"].pop("turn")
        row["properties"].pop("scenario_ids")
        row["properties"]["items"] = {
            "type": "array",
            "items": obj(
                {
                    "group": {"enum": list(GROUPS)},
                    "index": {"type": "integer", "minimum": 0},
                }
            ),
        }
        row["required"] = list(row["properties"])
        # Exact required keys eliminate customer-only renumbering and omissions.
        self.schema["properties"]["source_coverage"] = obj(
            {key: deepcopy(row) for key in self.sources}
        )
        self.verification_schema = obj(
            {
                "verdict": {"enum": ["supported", "corrected", "needs_input"]},
                "reason": {"type": "string", "minLength": 1},
                "corrections": obj(
                    {
                        key: {"anyOf": [{"type": "null"}, field]}
                        for key, field in self.schema["properties"].items()
                    }
                ),
            }
        )
        self.verification_schema["description"] = (
            "Review envelope: verdict and reason are top-level fields beside corrections. "
            "All fixture fields belong inside corrections; null preserves the draft field."
        )

    @property
    def evidence(self):
        lookup = {i: key for key, (i, _) in self.sources.items()}
        return {
            "source_conversation": [
                {**t, "source_index": i, "customer_turn_id": lookup.get(i)}
                for i, t in enumerate(self.turns)
            ],
            "customer_turn_catalog": [
                {"turn_id": key, "source_index": i, "text": text}
                for key, (i, text) in self.sources.items()
            ],
        }

    def encode(self, plan):
        result = deepcopy(plan)
        refs = {
            item["id"]: {"group": group, "index": i}
            for group in GROUPS
            for i, item in enumerate(plan[group])
        }
        for group in GROUPS:
            for item in result[group]:
                item.pop("id")
                if group != "actions":
                    quote = item.pop("quote")
                    item["quote_turn_id"] = (
                        None
                        if group == "facts" and item["provenance"] == "test_assumption"
                        else next(
                            key
                            for key, (_, text) in self.sources.items()
                            if quote.strip() and original_quote(quote, text) is not None
                        )
                    )
        turns = {i: key for key, (i, _) in self.sources.items()}
        result["source_coverage"] = {
            turns[row["turn"]]: {
                "disposition": row["disposition"],
                "reason": row["reason"],
                "items": [refs[key] for key in row["scenario_ids"]],
            }
            for row in plan["source_coverage"]
        }
        return result

    def decode(self, wire, original=None):
        encoded = self.encode(original) if original is not None else None
        if encoded == wire:
            return deepcopy(original)
        result = deepcopy(wire)
        unchanged = {g for g in GROUPS if encoded is not None and wire[g] == encoded[g]}
        used = {item["id"] for g in unchanged for item in original[g]}
        for group in GROUPS:
            if group in unchanged:
                result[group] = deepcopy(original[group])
                continue
            for i, item in enumerate(result[group]):
                key = f"{group[0]}_{i}"
                while key in used:
                    key += "_new"
                item["id"] = key
                used.add(key)
                if group != "actions":
                    ref = item.pop("quote_turn_id")
                    item["quote"] = self.sources[ref][1] if ref is not None else ""
        result["source_coverage"] = [
            {
                "turn": index,
                "disposition": wire["source_coverage"][key]["disposition"],
                "reason": wire["source_coverage"][key]["reason"],
                "scenario_ids": [
                    result[r["group"]][r["index"]]["id"]
                    for r in wire["source_coverage"][key]["items"]
                ],
            }
            for key, (index, _) in self.sources.items()
        ]
        return result

    def issues(self, wire):
        errors = schema_issues(wire, self.schema)
        if not isinstance(wire, dict):
            return errors

        def valid(group):
            return not schema_issues(wire.get(group), self.schema["properties"][group])

        for group in ("facts", "goals"):
            if not valid(group):
                continue
            for i, item in enumerate(wire[group]):
                quoted = group == "goals" or item["provenance"] == "customer_quote"
                ref = item["quote_turn_id"]
                if quoted != (ref is not None) or (
                    ref is not None and not self.sources[ref][1].strip()
                ):
                    errors.append(
                        issue(
                            [group, i, "quote_turn_id"],
                            "客户事实必须选择原话；测试假设不得冒充原话",
                            allowed=list(self.sources) if quoted else [None],
                        )
                    )
                if not quoted and not item["reason"].strip():
                    errors.append(issue([group, i, "reason"], "测试假设必须说明依据"))
        coverage = wire.get("source_coverage")
        if isinstance(coverage, dict) and all(valid(group) for group in GROUPS):
            for key, row in coverage.items():
                if key not in self.sources or schema_issues(
                    row, self.schema["properties"]["source_coverage"]["properties"][key]
                ):
                    continue
                for i, ref in enumerate(row["items"]):
                    if ref["index"] >= len(wire[ref["group"]]):
                        errors.append(
                            issue(
                                ["source_coverage", key, "items", i],
                                "原文映射引用了不存在的场景项目",
                                allowed=[
                                    {"group": group, "index": n}
                                    for group in GROUPS
                                    for n in range(len(wire[group]))
                                ],
                            )
                        )
                if (
                    row["disposition"] in {"fact", "goal", "action"}
                    and not row["items"]
                ):
                    errors.append(
                        issue(
                            ["source_coverage", key, "items"],
                            "实质客户信息不能丢失，必须映射到场景项目",
                        )
                    )
        if valid("initial_state") and valid("actions"):
            errors.extend(state_issues(wire))
        return errors


GUIDANCE = """
Use the supplied customer_turn_catalog and typed wire schema. Never count or renumber
customer turns yourself. source_coverage is an object with exactly the supplied turn
IDs as keys. Each material turn selects items by their group and zero-based position
in YOUR facts/goals/actions arrays. The server assigns canonical scenario IDs and
binds quote_turn_id to the exact original customer utterance. Do not author IDs,
quotes or numeric source indices. Test assumptions use quote_turn_id=null.
State keys in action preconditions/effects must match declared initial_state keys.
Independent verification uses corrections: null means unchanged; otherwise replace
that named field using its exact schema. Keep all unaffected content and source
concerns. There are no JSON paths to invent and no fields outside the declared schema.
On validation failure use repair_fixture and its returned repair_id catalog. Only
rejected fields can change; valid content stays locked. Fix all reported issues.
"""


class FixtureSubmission:
    tool_name = "repair_fixture"
    validates_schema = True

    def __init__(self, codec, *, draft=None, checkpoint=None, save=None, original=None):
        self.codec, self.base, self.save = codec, deepcopy(draft), save
        self.original = deepcopy(original)
        self.schema = codec.verification_schema if draft is not None else codec.schema
        self.draft = deepcopy(checkpoint)
        self.errors = self._issues(self.draft) if self.pending else []
        self.report = None

    @property
    def pending(self):
        return self.draft is not None

    @property
    def draft_id(self):
        return fingerprint(self.draft)

    def _candidate(self, arguments):
        if self.base is None:
            return arguments
        result = deepcopy(self.base)
        result.update(
            {
                key: value
                for key, value in arguments["corrections"].items()
                if value is not None
            }
        )
        return result

    def _issues(self, arguments):
        if self.base is None:
            return self.codec.issues(arguments)
        errors = schema_issues(arguments, self.schema)
        if errors:
            return errors
        if self.base is not None:
            corrections = arguments["corrections"]
            if arguments["verdict"] == "supported" and any(
                v is not None for v in corrections.values()
            ):
                errors.append(
                    issue(
                        ["verdict"],
                        "存在修正时必须标记 corrected，不能声称原草案完全正确",
                    )
                )
            for error in self.codec.issues(self._candidate(arguments)):
                path = error["path"]
                errors.append(
                    {
                        **error,
                        "path": ["corrections", *path]
                        if corrections[path[0]] is not None
                        else ["corrections", path[0]],
                    }
                )
            if arguments["verdict"] == "needs_input" and not any(
                x.get("blocks_execution")
                for x in self._candidate(arguments)["missing_information"]
            ):
                errors.append(
                    issue(
                        ["corrections", "missing_information"],
                        "需要补充信息时必须保留具体问题和阻塞原因",
                    )
                )
            return errors
        return self.codec.issues(arguments)

    def _catalog(self):
        return {"field_" + fingerprint(e["path"])[:16]: e for e in self.errors}

    def _error(self, progressed=False):
        return FixtureErrors(
            [
                {
                    **e,
                    "repair_id": "field_" + fingerprint(e["path"])[:16],
                    "value_schema": self._value_schema(e),
                }
                for e in self.errors
            ],
            draft_id=self.draft_id,
            progressed=progressed,
        )

    def _value_schema(self, error):
        if error.get("remove"):
            return {"type": "null"}
        schema = field_schema(self.schema, error["path"], self.draft)
        if "allowed" in error and all(
            jsonschema.Draft202012Validator(schema).is_valid(value)
            for value in error["allowed"]
        ):
            schema["enum"] = deepcopy(error["allowed"])
        return schema

    def spec(self):
        return {
            "name": self.tool_name,
            "description": "Repair only rejected fixture fields using the returned repair_id; preserve every valid field.",
            "inputSchema": obj(
                {
                    "draft_id": {"type": "string"},
                    "patches": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "anyOf": [
                                obj(
                                    {
                                        "repair_id": {"enum": [key]},
                                        "value": self._value_schema(error),
                                    }
                                )
                                for key, error in self._catalog().items()
                            ]
                        }
                        if self.pending
                        else obj(
                            {"repair_id": {"type": "string"}, "value": {"type": "null"}}
                        ),
                    },
                }
            ),
        }

    def _accept(self, arguments):
        plan = self.codec.decode(self._candidate(arguments), self.original)
        self.report = (
            plan
            if self.base is None
            else {
                "verdict": arguments["verdict"],
                "reason": arguments["reason"],
                "scenario": plan,
            }
        )
        self.draft, self.errors = None, []
        return deepcopy(self.report)

    def __call__(self, arguments):
        if self.pending:
            raise self._error()
        errors = self._issues(arguments)
        if errors:
            self.draft, self.errors = deepcopy(arguments), errors
            raise self._error()
        report = self._accept(arguments)
        arguments.clear()
        arguments.update(report)

    def repair(self, arguments):
        # Check the envelope separately so stale/duplicate IDs retain actionable
        # errors, and validate values before mutating even the saved draft copy.
        jsonschema.validate(
            arguments,
            obj(
                {
                    "draft_id": {"type": "string"},
                    "patches": {
                        "type": "array",
                        "minItems": 1,
                        "items": obj({"repair_id": {"type": "string"}, "value": {}}),
                    },
                }
            ),
        )
        if not self.pending or arguments["draft_id"] != self.draft_id:
            raise WorkbenchError("场景修复版本已失效；使用最近返回的 draft_id")
        result = deepcopy(self.draft)
        catalog, seen = self._catalog(), set()
        for patch in arguments["patches"]:
            key = patch["repair_id"]
            if key not in catalog or key in seen:
                raise WorkbenchError("只能修复本轮拒绝的字段，不能重复或修改有效内容")
            seen.add(key)
            error = catalog[key]
            jsonschema.validate(patch["value"], self._value_schema(error))
            path = error["path"]
            if not path:
                result = deepcopy(patch["value"])
                continue
            parent = result
            for part in path[:-1]:
                parent = parent[part]
            if error.get("remove"):
                if patch["value"] is not None:
                    raise WorkbenchError("额外字段只能用 null 删除")
                del parent[path[-1]]
            else:
                parent[path[-1]] = deepcopy(patch["value"])
        errors = self._issues(result)
        if errors:
            before = {tuple(e["path"]) for e in self.errors}
            self.draft, self.errors = result, errors
            after = {tuple(e["path"]) for e in errors}
            # Refining an invalid container into rejected leaves also locks
            # newly valid siblings. It is real progress, not a fresh retry loop.
            progressed = after != before and all(
                any(path[: len(old)] == old for old in before) for path in after
            )
            raise self._error(progressed)
        return self._accept(result)

    def resume_instructions(self):
        if not self.pending:
            return ""
        return "\nResume the saved fixture draft with repair_fixture: " + json.dumps(
            {**self._error().feedback(), "draft": self.draft}, ensure_ascii=False
        )

"""Keep candidate defaults and prompt references together without inventing values."""

import copy

import jsonschema

from api.services.agent_workbench.errors import WorkbenchError
from api.services.workflow.workflow_graph import extract_template_variables
from api.utils.template_renderer import is_builtin_variable

VARIABLE_UPDATES_SCHEMA = {
    "type": "object",
    "description": (
        "Template variable defaults to merge into this candidate. Use values supported "
        "by the user's request or selected attachments. Use an empty string for unknown "
        "values; use simulation case initial_context for test-only values. Existing "
        "defaults are preserved when omitted. Built-in and dotted runtime paths are "
        "not configurable here."
    ),
    "maxProperties": 200,
    "propertyNames": {
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^[^{}|\s.]+$",
    },
    "additionalProperties": {"type": "string", "maxLength": 20000},
}


def referenced_variables(graph):
    """Follow the same placeholder semantics as the actual workflow renderer."""
    names = set()

    def walk(value):
        if isinstance(value, str):
            names.update(extract_template_variables(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for item in [*graph.get("nodes", []), *graph.get("edges", [])]:
        walk(item.get("data", {}))
    return sorted(names)


def candidate_variables(graph, existing, updates=None):
    """Merge explicit defaults, then register unresolved references as empty rows."""
    if updates is not None:
        jsonschema.validate(updates, VARIABLE_UPDATES_SCHEMA)
        reserved = [
            name
            for name in updates
            if is_builtin_variable(name)
            or name in {"campaign_id", "provider", "source_uuid"}
        ]
        if reserved:
            raise WorkbenchError(
                "These variables are supplied at runtime: " + ", ".join(reserved)
            )
    variables = copy.deepcopy(existing or {})
    variables.update(updates or {})
    for name in referenced_variables(graph):
        variables.setdefault(name, "")
    if len(variables) > 200:
        raise WorkbenchError("A candidate may contain at most 200 template variables")
    return variables


def variable_report(snapshot):
    variables = snapshot.get("template_context_variables") or {}
    references = referenced_variables(snapshot["workflow_json"])
    return {
        "template_context_variables": variables,
        "referenced_variables": references,
        "missing_variables": [
            name
            for name in references
            if variables.get(name) is None or not str(variables[name]).strip()
        ],
    }

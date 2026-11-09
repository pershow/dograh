"""Small workbench read projections; large evidence stays in PostgreSQL.

Project before the asyncpg JSON decoder sees the data. Trimming ORM objects
after loading them still blocks the API loop on decoding and deep copies.
"""

from datetime import datetime

from sqlalchemy import ARRAY, JSON, Text, cast, column, func, literal_column, select
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by

from api.db.models import (
    WorkbenchAssetModel,
    WorkbenchEvaluationModel,
    WorkbenchJobModel,
    WorkbenchSuiteModel,
)

PRIVATE_JOB_OUTPUT = (
    "_orchestration",
    "_run_evidence",
    "_requested_target",
    "_task_basis",
    "_submitted_input",
)


def mapped_record(row):
    # These values were just decoded for this request; no ORM-owned containers.
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def _array_index(owner, value, fields, *, empty_evidence=False):
    entries = (
        func.jsonb_array_elements(cast(value, JSONB))
        .table_valued(column("value", JSONB), with_ordinality="position")
        .render_derived()
    )
    pairs = [part for key in fields for part in (key, entries.c.value[key])]
    if empty_evidence:
        for key in ("transcript", "assertions", "tool_calls"):
            pairs.extend((key, literal_column("'[]'::json", JSON)))
    return (
        select(
            func.coalesce(
                func.json_agg(
                    aggregate_order_by(
                        func.json_build_object(*pairs), entries.c.position
                    )
                ),
                literal_column("'[]'::json", JSON),
            )
        )
        .select_from(entries)
        .correlate(owner)
        .scalar_subquery()
    )


def evaluation_select(*, compact):
    model = WorkbenchEvaluationModel
    results = (
        _array_index(
            model,
            model.results,
            ("name", "status", "run_id", "source", "error", "review_pending"),
            empty_evidence=True,
        )
        if compact
        else model.results
    )
    # An opaque cache token, not an identity or content hash. PostgreSQL row
    # versions change on UPDATE; ctid also distinguishes updates in one tx.
    # https://www.postgresql.org/docs/current/ddl-system-columns.html
    revision = func.concat(
        "row-v1:",
        model.id,
        ":",
        cast(literal_column("workbench_evaluations.xmin"), Text),
        ":",
        cast(literal_column("workbench_evaluations.ctid"), Text),
    ).label("detail_revision")
    return select(
        *[c for c in model.__table__.columns if c.name != "results"],
        results.label("results"),
        revision,
    )


def compact_select(model):
    if model is WorkbenchEvaluationModel:
        return evaluation_select(compact=True)
    replacements = {}
    if model is WorkbenchAssetModel:
        # The compact HTTP response removes parsed content. Keep only the kind
        # used by voice status; full attachment reads use wb_get instead.
        replacements["parsed"] = func.json_build_object("kind", model.parsed["kind"])
    elif model is WorkbenchSuiteModel:
        cases = _array_index(model, model.payload["cases"], ("name",))
        replacements["payload"] = func.jsonb_set(
            cast(model.payload, JSONB), cast(["cases"], ARRAY(Text)), cast(cases, JSONB)
        )
    elif model is WorkbenchJobModel:
        output = cast(model.output, JSONB)
        for key in PRIVATE_JOB_OUTPUT:
            output = output.op("-")(cast(key, Text))
        replacements["output"] = output
    return select(
        *[
            replacements[c.name].label(c.name) if c.name in replacements else c
            for c in model.__table__.columns
        ]
    )


def compact_record(row, model):
    result = mapped_record(row)
    if model in (WorkbenchEvaluationModel, WorkbenchSuiteModel):
        result["summary_only"] = True
    return result

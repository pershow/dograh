"""Count persisted case evidence consistently before and during execution."""


def retained_result(result):
    return {
        **result,
        "runtime_responded": bool(result.get("runtime_responded"))
        or any(
            m.get("role") == "assistant" and m.get("text")
            for m in result.get("transcript", [])
        ),
        "reused_runtime": bool(result.get("run_id")),
    }


def result_counts(values):
    from api.services.agent_workbench.review import review_counts

    return {
        **{
            status: sum(r["status"] == status for r in values)
            for status in ("passed", "failed", "error", "inconclusive")
        },
        **review_counts(values),
        **{
            key: sum(r.get("validity", {}).get("status") == status for r in values)
            for key, status in (
                ("valid", "valid"),
                ("invalid", "invalid"),
                ("validity_inconclusive", "inconclusive"),
            )
        },
        "attempted": sum(bool(r.get("run_id")) for r in values),
        "executed": sum(bool(r.get("runtime_responded")) for r in values),
        "reused": sum(bool(r.get("reused_runtime")) for r in values),
    }

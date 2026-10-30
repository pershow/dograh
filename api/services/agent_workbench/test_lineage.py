"""Explicit test ancestry, independent of dialogue wording and model guesses."""


def parent_ids(job):
    payload, output = job.get("input", {}), job.get("output", {})
    if payload.get("rerun_test_job_id"):
        return set()  # A fresh replay is a new result lineage.
    return {
        value
        for value in (
            payload.get("continue_test_job_id"),
            payload.get("resume_test_job_id"),
            payload.get("test_source_job_id"),
            output.get("test_source_job_id"),
        )
        if value and value != job.get("id")
    }


def same_scope(left, right):
    """Never combine different frozen versions or explicitly selected sources."""
    a, b = left.get("input", {}), right.get("input", {})
    x, y = left.get("output", {}), right.get("output", {})
    for key in ("requested_target_fingerprint", "test_target_fingerprint"):
        if x.get(key) and y.get(key) and x[key] != y[key]:
            return False
    for key in ("candidate_id", "test_target_session_id", "test_mode"):
        if a.get(key) and b.get(key) and a[key] != b[key]:
            return False
    for key in ("asset_ids", "test_suite_ids"):
        if a.get(key) and b.get(key) and set(a[key]) != set(b[key]):
            return False
    return True


def test_chain(records, source_id):
    """Return a connected task family in supplied newest-first order.

    Parents may lie outside a compact history page. Shared explicit parent IDs
    still connect its visible continuations. No text similarity joins tasks.
    """
    indexed = {r["id"]: r for r in records}
    if source_id not in indexed:
        return []
    source = indexed[source_id]
    eligible = [
        r
        for r in records
        if same_scope(source, r)
        and (
            r["id"] == source_id
            or (
                r.get("kind", "build") == "build"
                and r.get("input", {}).get("mode") not in {"build", "analyze"}
                and r.get("output", {}).get("outcome")
                not in {"build", "analysis", "review"}
            )
        )
    ]
    connected = {source_id}
    found = set()
    while True:
        added = False
        for row in eligible:
            links = {row["id"], *parent_ids(row)}
            if row["id"] not in found and links & connected:
                found.add(row["id"])
                connected.update(links)
                added = True
        if not added:
            return [r for r in eligible if r["id"] in found]


def chain_heads(records):
    """Expose only the latest state of each task chain to contextual routing."""
    covered, heads = set(), []
    for row in records:
        if row["id"] in covered:
            continue
        chain = test_chain(records, row["id"])
        covered.update(r["id"] for r in chain)
        heads.append(row)
    return heads

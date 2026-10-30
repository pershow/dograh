"""Compare observed runs without treating missing audio or outcomes as zero."""

import math


def percentile(values, fraction):
    ordered = sorted(
        v
        for v in values
        if isinstance(v, (float, int))
        and not isinstance(v, bool)
        and math.isfinite(v)
        and v >= 0
    )
    return (
        ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))]
        if ordered
        else None
    )


def summarize_runs(runs):
    groups = {}
    for run in runs:
        context = run.get("gathered_context") or {}
        profile = context.get("runtime_profile") or run.get("runtime_profile") or {}
        key = (
            profile.get("channel", "unknown"),
            profile.get("pipeline", "unknown"),
            profile.get("fingerprint", "unknown"),
        )
        group = groups.setdefault(
            key,
            {
                "channel": key[0],
                "pipeline": key[1],
                "fingerprint": key[2],
                "runs": 0,
                "runs_with_evidence": 0,
                "missing_evidence": 0,
                "incomplete_evidence": 0,
                "silent_turns": 0,
                "errors": 0,
                "samples": [],
                "first_audio_samples": [],
                "successful_tasks": 0,
                "outcome_unknown": 0,
                "cost_missing": 0,
                "total_cost_usd": 0.0,
            },
        )
        group["runs"] += 1
        task = context.get("task_state") or {}
        group["successful_tasks"] += task.get("status") == "completed"
        group["outcome_unknown"] += task.get("status") not in {"completed", "failed"}
        cost = (run.get("cost_info") or {}).get("total_cost_usd")
        if (
            isinstance(cost, (float, int))
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
        ):
            group["total_cost_usd"] += cost
        else:
            group["cost_missing"] += 1
        evidence = context.get("runtime_evidence")
        if not evidence:
            group["missing_evidence"] += 1
            continue
        group["runs_with_evidence"] += 1
        group["incomplete_evidence"] += bool(evidence.get("dropped"))
        for event in evidence.get("events", []):
            if event["event"] == "latency_breakdown":
                value = (event.get("detail") or {}).get("e2e_ms")
                if (
                    isinstance(value, (float, int))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value >= 0
                ):
                    group["samples"].append(value)
                first_audio = (event.get("detail") or {}).get("turn_to_audio_ms")
                if (
                    isinstance(first_audio, (float, int))
                    and not isinstance(first_audio, bool)
                    and math.isfinite(first_audio)
                    and first_audio >= 0
                ):
                    group["first_audio_samples"].append(first_audio)
            group["silent_turns"] += event["event"] == "bot_silent_after_user_turn"
            group["errors"] += event["event"] == "pipeline_error"
    results = []
    for group in groups.values():
        values = group.pop("samples")
        first_audio = group.pop("first_audio_samples")
        cost_per_success = (
            group["total_cost_usd"] / group["successful_tasks"]
            if not group["cost_missing"]
            and not group["outcome_unknown"]
            and group["successful_tasks"]
            else None
        )
        results.append(
            {
                **group,
                "latency_samples": len(values),
                "p50_ms": percentile(values, 0.5),
                "p95_ms": percentile(values, 0.95),
                "measurement": "server_bot_speech_start",
                "first_generated_audio_p95_ms": percentile(first_audio, 0.95),
                "meaningful_response_ms": None,
                "cost_per_success": cost_per_success,
                "cost_unit": "USD",
                "outcome_source": "structured_task_stage",
            }
        )
    return results

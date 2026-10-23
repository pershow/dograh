"""A worker with an incompatible Codex runtime must not dispatch queued work."""

from unittest.mock import AsyncMock

import pytest

from api.services.codex.runtime import CodexError
from api.tasks import agent_workbench as worker


async def test_disabled_worker_does_not_dispatch_outbox(monkeypatch):
    monkeypatch.setenv("AGENT_WORKBENCH_ENABLED", "false")
    dispatch = AsyncMock()
    monkeypatch.setattr(worker, "dispatch_workbench_outbox", dispatch)
    with pytest.raises(RuntimeError, match="AGENT_WORKBENCH_ENABLED"):
        await worker.startup({})
    dispatch.assert_not_awaited()


async def test_failed_runtime_probe_does_not_dispatch_outbox(monkeypatch):
    monkeypatch.setenv("AGENT_WORKBENCH_ENABLED", "true")
    probe = AsyncMock(side_effect=CodexError("incompatible build"))
    dispatch = AsyncMock()
    monkeypatch.setattr(worker, "probe_runtime", probe)
    monkeypatch.setattr(worker, "dispatch_workbench_outbox", dispatch)
    with pytest.raises(CodexError, match="incompatible build"):
        await worker.startup({"workbench_unbounded": True})
    dispatch.assert_not_awaited()


async def test_worker_probes_before_dispatching_outbox(monkeypatch):
    monkeypatch.setenv("AGENT_WORKBENCH_ENABLED", "true")
    events = []

    async def probe(_binary):
        events.append("probe")

    async def dispatch(_ctx):
        events.append("dispatch")

    monkeypatch.setattr(worker, "probe_runtime", probe)
    monkeypatch.setattr(worker, "dispatch_workbench_outbox", dispatch)
    await worker.startup({"workbench_unbounded": True})
    assert events == ["probe", "dispatch"]


async def test_worker_has_no_execution_deadline_but_keeps_a_finite_lease():
    instance = worker.create_workbench_worker(handle_signals=False)
    assert instance.job_timeout_s is None
    assert instance.in_progress_timeout_s == 70
    assert instance.ctx["workbench_unbounded"] is True
    assert instance.cron_jobs[0].timeout_s == 60


async def test_legacy_entry_point_cannot_silently_reintroduce_deadline(monkeypatch):
    monkeypatch.setenv("AGENT_WORKBENCH_ENABLED", "true")
    with pytest.raises(RuntimeError, match="python -m api.tasks.agent_workbench"):
        await worker.startup({})


async def test_unbounded_job_renews_queue_lock_and_stops_when_cancelled(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from api.services.agent_workbench import jobs
    from api.services.agent_workbench.errors import LeaseLost

    released = asyncio.Event()
    job = {"id": "job", "lease_token": "lease"}
    monkeypatch.setattr(jobs.db_client, "wb_claim", AsyncMock(return_value=job))
    monkeypatch.setattr(
        jobs.db_client, "wb_heartbeat", AsyncMock(side_effect=[None, LeaseLost()])
    )
    finish = AsyncMock()
    monkeypatch.setattr(jobs.db_client, "wb_finish", finish)

    async def perform(*_):
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    monkeypatch.setattr(jobs, "_perform", perform)
    original_sleep = asyncio.sleep
    original_wait = asyncio.wait

    async def sleep(_):
        await original_sleep(0)

    async def wait(*args, **kwargs):
        assert kwargs["timeout"] is None
        return await original_wait(*args, **kwargs)

    monkeypatch.setattr(
        jobs,
        "asyncio",
        SimpleNamespace(
            create_task=asyncio.create_task,
            sleep=sleep,
            wait=wait,
            FIRST_COMPLETED=asyncio.FIRST_COMPLETED,
            gather=asyncio.gather,
        ),
    )
    redis = SimpleNamespace(expire=AsyncMock())
    await asyncio.wait_for(
        jobs.execute_workbench_job(
            {"redis": redis, "job_id": "queue-job"}, 1, "session", "job"
        ),
        1,
    )
    redis.expire.assert_awaited_once_with("arq:in-progress:queue-job", 70)
    assert released.is_set()
    finish.assert_not_awaited()

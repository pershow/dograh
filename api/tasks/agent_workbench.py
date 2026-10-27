"""Run with: python -m api.tasks.agent_workbench."""

import asyncio
from typing import ClassVar

from arq import cron
from arq.worker import create_worker

from api.constants import REDIS_URL
from api.services.agent_workbench.config import WorkbenchSettings
from api.services.agent_workbench.jobs import (
    QUEUE,
    dispatch_workbench_outbox,
    execute_workbench_job,
)
from api.services.codex.probe import probe_runtime
from api.services.pipecat.tracing_config import (
    handle_langfuse_sync,
    load_all_org_langfuse_credentials,
)
from api.services.worker_sync.manager import WorkerSyncManager
from api.services.worker_sync.protocol import WorkerSyncEventType
from api.tasks.arq import REDIS_SETTINGS


async def startup(ctx):
    settings = WorkbenchSettings.load()
    if not settings.enabled:
        raise RuntimeError("AGENT_WORKBENCH_ENABLED must be true for this worker.")
    if not ctx.get("workbench_unbounded"):
        raise RuntimeError(
            "Start this worker with: python -m api.tasks.agent_workbench"
        )
    await probe_runtime(settings.codex_binary)
    # This is a separate process from FastAPI; its exporters and credential
    # subscriptions must be initialized here too, before simulations start.
    await load_all_org_langfuse_credentials()
    sync = WorkerSyncManager(REDIS_URL)
    sync.register(WorkerSyncEventType.LANGFUSE_CREDENTIALS, handle_langfuse_sync)
    ctx["workbench_sync"] = sync
    try:
        await sync.start()
        await dispatch_workbench_outbox(ctx)
    except BaseException:
        await shutdown(ctx)
        raise


async def shutdown(ctx):
    from opentelemetry import trace

    try:
        sync = ctx.pop("workbench_sync", None)
        if sync:
            await sync.stop()
    finally:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            await asyncio.to_thread(provider.force_flush, timeout_millis=10000)


class WorkbenchWorkerSettings:
    functions: ClassVar = [execute_workbench_job]
    cron_jobs: ClassVar = [
        cron(dispatch_workbench_outbox, second={0, 15, 30, 45}, timeout=60)
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = REDIS_SETTINGS
    queue_name = QUEUE
    max_jobs = 4
    # ARQ uses this finite value to initialize its Redis in-progress lease.
    # create_workbench_worker removes the execution deadline after construction.
    job_timeout = 60
    keep_result = 0


def create_workbench_worker(**kwargs):
    kwargs["ctx"] = {**kwargs.get("ctx", {}), "workbench_unbounded": True}
    worker = create_worker(WorkbenchWorkerSettings, **kwargs)
    worker.job_timeout_s = None
    return worker


if __name__ == "__main__":
    from logging.config import dictConfig

    from api.tasks.arq import LOG_CONFIG

    dictConfig(LOG_CONFIG)
    create_workbench_worker().run()

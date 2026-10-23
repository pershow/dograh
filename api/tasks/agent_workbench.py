"""Run with: python -m api.tasks.agent_workbench."""

from typing import ClassVar

from arq import cron
from arq.worker import create_worker

from api.services.agent_workbench.config import WorkbenchSettings
from api.services.agent_workbench.jobs import (
    QUEUE,
    dispatch_workbench_outbox,
    execute_workbench_job,
)
from api.services.codex.probe import probe_runtime
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
    await dispatch_workbench_outbox(ctx)


class WorkbenchWorkerSettings:
    functions: ClassVar = [execute_workbench_job]
    cron_jobs: ClassVar = [
        cron(dispatch_workbench_outbox, second={0, 15, 30, 45}, timeout=60)
    ]
    on_startup = startup
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

"""Run with: arq api.tasks.agent_workbench.WorkbenchWorkerSettings."""

from typing import ClassVar

from arq import cron

from api.services.agent_workbench.jobs import (
    QUEUE,
    dispatch_workbench_outbox,
    execute_workbench_job,
)
from api.tasks.arq import REDIS_SETTINGS


class WorkbenchWorkerSettings:
    functions: ClassVar = [execute_workbench_job]
    cron_jobs: ClassVar = [cron(dispatch_workbench_outbox, second={0, 15, 30, 45})]
    on_startup = dispatch_workbench_outbox
    redis_settings = REDIS_SETTINGS
    queue_name = QUEUE
    max_jobs = 4
    job_timeout = 960
    keep_result = 0

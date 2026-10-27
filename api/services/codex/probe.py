"""No-model startup probe: exercise the real initialize/thread/dynamic-tool API."""

import tempfile
from pathlib import Path

from .configuration import prepare_home
from .connection import CodexConnection
from .runtime import CodexError


async def probe_runtime(binary):
    async def reject_request(_method, _params):
        raise CodexError("No tools are callable during startup.")

    async def ignore_notification(_method, _params):
        pass

    with tempfile.TemporaryDirectory(prefix="dograh-codex-check-") as directory:
        workdir, env = prepare_home(
            Path(directory),
            model="dograh-probe",
            base_url="http://127.0.0.1:9/v1",
            token="startup-probe",
        )
        rpc = CodexConnection(reject_request, ignore_notification, request_timeout=20)
        try:
            await rpc.start(binary, cwd=workdir, env=env)
            response = await rpc.request(
                "thread/start",
                {
                    "model": "dograh-probe",
                    "modelProvider": "dograh",
                    "cwd": str(workdir),
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "baseInstructions": "Use only Dograh dynamic tools.",
                    "dynamicTools": [
                        {
                            "name": "dograh_probe",
                            "description": "Startup protocol check.",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ],
                },
            )
            thread = response.get("thread") if isinstance(response, dict) else None
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise CodexError(
                    "Codex thread/start returned an incompatible response."
                )
        finally:
            await rpc.close()

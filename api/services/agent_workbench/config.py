import os
from dataclasses import dataclass
from pathlib import Path

from api.services.codex.runtime import CodexError, default_binary, validate_runtime


@dataclass(frozen=True)
class WorkbenchSettings:
    enabled: bool
    codex_binary: str
    root: Path
    timeout_seconds: int | None = None
    max_model_requests: int | None = None
    max_model_tokens: int | None = None
    max_output_tokens: int = 32768
    model_idle_timeout: int = 90
    model_request_timeout: int | None = None
    model_max_retries: int = 2
    build_repair_rounds: int | None = None
    evaluation_concurrency: int = 3
    model_context_window: int = 131072

    @classmethod
    def load(cls):
        return cls(
            enabled=os.getenv("AGENT_WORKBENCH_ENABLED", "false").lower() == "true",
            codex_binary=os.getenv("WORKBENCH_CODEX_BINARY") or default_binary(),
            root=Path(
                os.getenv(
                    "WORKBENCH_DATA_DIR",
                    str(Path.home() / ".local/share/dograh/workbench"),
                )
            ),
            model_context_window=max(
                8192, int(os.getenv("WORKBENCH_MODEL_CONTEXT_WINDOW", "131072"))
            ),
            # Cumulative usage is telemetry, not a default task cutoff.
            # A deployment may explicitly opt into a cap; 0 means unlimited.
            max_model_requests=(
                max(0, int(os.getenv("WORKBENCH_MAX_MODEL_REQUESTS", "0"))) or None
            ),
            max_model_tokens=(
                max(0, int(os.getenv("WORKBENCH_MAX_MODEL_TOKENS", "0"))) or None
            ),
            max_output_tokens=max(
                1, min(131072, int(os.getenv("WORKBENCH_MAX_OUTPUT_TOKENS", "32768")))
            ),
            model_idle_timeout=max(
                1, min(300, int(os.getenv("WORKBENCH_MODEL_IDLE_TIMEOUT", "90")))
            ),
            # No wall-clock cutoff while a model is still producing output.
            # Deployments can opt into a request deadline with a positive value.
            model_request_timeout=(
                max(0, int(os.getenv("WORKBENCH_MODEL_REQUEST_TIMEOUT", "0"))) or None
            ),
            model_max_retries=max(
                0, min(3, int(os.getenv("WORKBENCH_MODEL_MAX_RETRIES", "2")))
            ),
            # No arbitrary round cutoff by default. The repair loop stops on
            # convergence, infrastructure failure or repeated lack of progress.
            # Preserve explicit deployment caps, including 0 to disable repairs.
            build_repair_rounds=(
                max(0, int(os.environ["WORKBENCH_BUILD_REPAIR_ROUNDS"]))
                if os.getenv("WORKBENCH_BUILD_REPAIR_ROUNDS", "").strip()
                else None
            ),
            evaluation_concurrency=max(
                1, min(8, int(os.getenv("WORKBENCH_EVALUATION_CONCURRENCY", "3")))
            ),
        )

    def capabilities(self):
        from api import constants

        try:
            validate_runtime(self.codex_binary)
            installed = True
        except CodexError:
            installed = False
        return {
            "enabled": self.enabled,
            "mps_enabled": constants.MPS_ENABLED,
            "managed_service_bootstrap": constants.MPS_ENABLED
            and constants.ENABLE_MANAGED_SERVICE_BOOTSTRAP,
            "legacy_builder": constants.MPS_ENABLED,
            "build": self.enabled and installed,
            "voice": self.enabled,
            "attachments": self.enabled,
            "simulation": self.enabled,
            "codex_installed": installed,
            "model_source": "Dograh model connections",
            "voice_transport": "Dograh WebRTC",
            "supported_files": [
                "txt",
                "md",
                "csv",
                "json",
                "pdf",
                "docx",
                "xlsx",
                "png",
                "jpg",
                "jpeg",
                "webp",
            ],
            "max_file_bytes": 20 * 1024 * 1024,
            "max_attachments": 5,
        }

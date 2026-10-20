import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkbenchSettings:
    enabled: bool
    codex_binary: str
    root: Path
    timeout_seconds: int = 900
    max_model_requests: int = 40
    max_model_tokens: int = 150000

    @classmethod
    def load(cls):
        return cls(
            enabled=os.getenv("AGENT_WORKBENCH_ENABLED", "false").lower() == "true",
            codex_binary=os.getenv("WORKBENCH_CODEX_BINARY", "codex"),
            root=Path(os.getenv("WORKBENCH_DATA_DIR", "/tmp/dograh-workbench")),
            max_model_requests=max(
                1, int(os.getenv("WORKBENCH_MAX_MODEL_REQUESTS", "40"))
            ),
            max_model_tokens=max(
                1, int(os.getenv("WORKBENCH_MAX_MODEL_TOKENS", "150000"))
            ),
        )

    def capabilities(self):
        from api import constants

        installed = bool(shutil.which(self.codex_binary))
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

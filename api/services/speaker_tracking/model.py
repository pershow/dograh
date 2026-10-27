"""Pinned CAM++ model provisioning and CPU-only sherpa-onnx inference.

Run ``python -m api.services.speaker_tracking.model`` before serving calls.
No network requests are made from a call; a missing model disables diagnostics.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

MODEL_NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    f"speaker-recongition-models/{MODEL_NAME}"
)
MODEL_SHA256 = "f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11"
_lock = threading.Lock()
_extractor = None


def model_path() -> Path:
    configured = os.environ.get("SPEAKER_TRACKING_MODEL_PATH")
    return (
        Path(configured) if configured else Path.home() / ".cache/dograh" / MODEL_NAME
    )


def verify(path: Path):
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != MODEL_SHA256:
        raise ValueError("CAM++ model checksum mismatch")


def download_model() -> Path:
    path = model_path()
    if path.exists():
        verify(path)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
            temp = Path(target.name)
            with urllib.request.urlopen(MODEL_URL, timeout=60) as source:
                size = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > 30 * 1024 * 1024:
                        raise ValueError("Unexpected CAM++ model size")
                    target.write(chunk)
        verify(temp)
        temp.chmod(0o644)
        temp.replace(path)
        return path
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def load_extractor():
    """Called only from the bounded native executor; one model per process."""
    global _extractor
    with _lock:
        if _extractor is None:
            import sherpa_onnx

            path = model_path()
            verify(path)
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(path), num_threads=1, provider="cpu", debug=False
            )
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        return _extractor


def embed(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    import numpy as np
    import soxr

    extractor = load_extractor()
    if sample_rate != 16000:
        samples = soxr.resample(samples, sample_rate, 16000).astype(np.float32)
    # Do not rely on native extractor thread safety. Streams never cross calls.
    with _lock:
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=samples)
        stream.input_finished()
        if not extractor.is_ready(stream):
            raise ValueError("CAM++ stream not ready")
        return np.asarray(extractor.compute(stream), dtype=np.float32)


if __name__ == "__main__":
    print(download_model())

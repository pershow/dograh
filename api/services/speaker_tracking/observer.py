"""Read-only audio tap. No frames, transcripts or interruption signals are emitted."""

import asyncio
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection

from api.services.speaker_tracking.analyzer import CallAnalyzer
from api.services.speaker_tracking.model import MODEL_SHA256

# Admission is bounded *before* submitting native work. A cancelled await cannot
# release its slot while an ONNX invocation is still running in a thread.
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="speaker-shadow")
_slots = threading.BoundedSemaphore(2)


def submit_native(function, *args):
    if not _slots.acquire(blocking=False):
        return None
    try:
        future = _pool.submit(function, *args)
    except BaseException:
        _slots.release()
        raise
    future.add_done_callback(lambda _: _slots.release())
    return asyncio.wrap_future(future)


@dataclass(frozen=True)
class AudioSample:
    pcm: bytes
    rate: int
    end: float
    learn: bool
    generation: int
    received: float

    @property
    def seconds(self):
        return len(self.pcm) / (2 * self.rate)


class SpeakerTrackingObserver(BaseObserver):
    MAX_QUEUE_SECONDS = 3.0
    MAX_EVENTS = 2000

    def __init__(self, *, input_processor, logs_buffer, answer_supervisor=None):
        super().__init__()
        self._input = input_processor
        self._logs = logs_buffer
        self._supervisor = answer_supervisor
        self._queue: deque[AudioSample] = deque()
        self._available = asyncio.Event()
        self._queued_seconds = 0.0
        self._audio_end = 0.0
        self._generation = 0
        self._bot_speaking = False
        self._closed = False
        self._disabled = False
        self._worker = None
        self._analyzer = None
        self._counts = Counter()
        self._event_count = 0
        self._finish_lock = asyncio.Lock()

    async def on_push_frame(self, data: FramePushed):
        if self._closed or self._disabled:
            return
        if data.first_push and isinstance(data.frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif data.first_push and isinstance(data.frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        if (
            data.source is not self._input
            or data.direction != FrameDirection.DOWNSTREAM
            or not isinstance(data.frame, InputAudioRawFrame)
        ):
            return
        frame = data.frame
        if (
            frame.sample_rate not in (8000, 16000)
            or frame.num_channels != 1
            or len(frame.audio) % 2
        ):
            self._drop("unsupported_audio")
            return
        seconds = len(frame.audio) / (2 * frame.sample_rate)
        self._audio_end += seconds
        if self._supervisor is not None and self._supervisor.blocks_workflow:
            self._drop("answer_supervisor_pending")
            return
        if not seconds:
            return
        if (
            seconds > 1.0
            or len(self._queue) >= 150
            or self._queued_seconds + seconds > self.MAX_QUEUE_SECONDS
        ):
            self._drop("queue_overflow")
            return
        self._queue.append(
            AudioSample(
                bytes(frame.audio),
                frame.sample_rate,
                self._audio_end,
                not self._bot_speaking,
                self._generation,
                time.monotonic(),
            )
        )
        self._available.set()
        self._queued_seconds += seconds
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="speaker-shadow")

    def _drop(self, reason):
        self._counts[reason] += 1
        self._generation += 1

    async def _record(self, details: dict, *, force=False):
        self._counts[details["action"]] += 1
        if self._event_count >= self.MAX_EVENTS and not force:
            return
        self._event_count += 1
        try:
            await self._logs.append(
                {
                    "type": "rtf-speaker-tracking",
                    "mode": "shadow",
                    "version": 1,
                    "payload": details,
                },
                use_current_node=False,
                use_current_turn=False,
            )
        except Exception:  # noqa: BLE001 - diagnostic failures must be isolated
            # An optional log writer must never break or hold up the call.
            self._counts["log_error"] += 1
            logger.warning("Speaker shadow diagnostic could not be recorded")

    async def _run(self):
        previous_generation = -1
        last = None
        try:
            await self._record(
                {"action": "started", "model": "CAM++", "model_sha256": MODEL_SHA256}
            )
            while True:
                if not self._queue:
                    if self._closed:
                        if last and self._analyzer:
                            pending = submit_native(self._analyzer.flush, *last)
                            if pending is not None:
                                for result in await pending:
                                    await self._record(result)
                        return
                    self._available.clear()
                    await self._available.wait()
                    continue
                sample = self._queue.popleft()
                self._queued_seconds -= sample.seconds
                if time.monotonic() - sample.received > self.MAX_QUEUE_SECONDS:
                    self._drop("stale_audio")
                    previous_generation = -1
                    continue
                if self._analyzer is None:
                    started = time.monotonic()
                    pending = submit_native(CallAnalyzer)
                    if pending is None:
                        self._drop("executor_busy")
                        continue
                    self._analyzer = await pending
                    await self._record(
                        {
                            "action": "ready",
                            "initialization_ms": round(
                                (time.monotonic() - started) * 1000, 2
                            ),
                        }
                    )
                # Batch up to ~320 ms to reduce executor scheduling overhead.
                batch = [sample.pcm]
                duration = sample.seconds
                end = sample.end
                while duration < 0.32 and self._queue:
                    # Peek only on this event loop; never combine across gaps
                    # or a change in whether learning is allowed.
                    following = self._queue[0]
                    if (following.rate, following.learn, following.generation) != (
                        sample.rate,
                        sample.learn,
                        sample.generation,
                    ):
                        break
                    following = self._queue.popleft()
                    self._queued_seconds -= following.seconds
                    batch.append(following.pcm)
                    duration += following.seconds
                    end = following.end
                pending = submit_native(
                    self._analyzer.process,
                    b"".join(batch),
                    sample.rate,
                    end,
                    sample.learn,
                    previous_generation != sample.generation,
                )
                if pending is None:
                    self._drop("executor_busy")
                    previous_generation = -1
                    continue
                for result in await pending:
                    await self._record(
                        {
                            **result,
                            "analysis_lag_ms": round(
                                (time.monotonic() - sample.received) * 1000, 2
                            ),
                        }
                    )
                previous_generation = sample.generation
                last = (sample.rate, end, sample.learn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - disable only this optional observer
            self._disabled = True
            await self._record(
                {"action": "disabled", "reason": type(exc).__name__}, force=True
            )
            logger.warning("Speaker shadow analysis disabled ({})", type(exc).__name__)

    async def finish(self):
        """Bounded, idempotent drain before the run's logs are persisted."""
        async with self._finish_lock:
            if self._closed:
                return
            self._closed = True
            if self._worker:
                self._available.set()
                try:
                    # Completion persistence waits at most 500 ms, never speech.
                    await asyncio.wait_for(asyncio.shield(self._worker), timeout=0.5)
                except TimeoutError:
                    self._counts["finish_timeout"] += 1
                    self._worker.cancel()
                    await asyncio.gather(self._worker, return_exceptions=True)
                except asyncio.CancelledError:
                    # A diagnostic worker may already have been cancelled.
                    # Its cancellation must not cancel completion persistence.
                    # Preserve cancellation directed at this finishing task.
                    if not self._worker.cancelled():
                        raise
                    self._counts["worker_cancelled"] += 1
            discarded = len(self._queue)
            self._queue.clear()
            self._queued_seconds = 0
            self._analyzer = None
            self._worker = None
            await self._record(
                {
                    "action": "summary",
                    "counts": dict(self._counts),
                    "discarded_on_finish": discarded,
                    "audio_seconds": round(self._audio_end, 3),
                },
                force=True,
            )

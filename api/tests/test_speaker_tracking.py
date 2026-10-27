import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    TranscriptionFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from api.schemas.workflow_configurations import WorkflowConfigurationDefaults
from api.services.pipecat.in_memory_buffers import InMemoryLogsBuffer
from api.services.speaker_tracking import observer as module
from api.services.speaker_tracking.analyzer import CallAnalyzer
from api.services.speaker_tracking.tracker import SpeakerTracker

A = np.array([1, 0, 0], dtype=np.float32)
B = np.array([0, 1, 0], dtype=np.float32)


def observe(tracker, vector=A, end=1.6, learn=True):
    return tracker.observe(vector, speech_seconds=1.6, end=end, learn=learn)


def test_call_local_enrollment_and_sustained_handoff_and_return():
    tracker = SpeakerTracker()
    assert observe(tracker)["action"] == "candidate"
    assert tracker.primary is None
    observe(tracker, end=3.2)
    assert observe(tracker, end=4.8)["action"] == "primary_established"
    assert observe(tracker, B, 6.4)["action"] == "candidate"
    assert tracker.primary == "speaker_1"
    observe(tracker, B, 8)
    switch = observe(tracker, B, 9.6)
    assert switch["action"] == "primary_changed"
    assert switch["previous_primary"] == "speaker_1"
    for end in [11.2, 12.8, 14.4]:
        result = observe(tracker, A, end)
    assert result["action"] == "primary_changed"
    assert result["primary_speaker"] == "speaker_1"
    assert len(tracker.voices) == 2
    assert SpeakerTracker().voices == {}


def test_alternating_background_voice_does_not_steal_primary():
    tracker = SpeakerTracker()
    for end in [1.6, 3.2, 4.8]:
        observe(tracker, end=end)
    for i in range(20):
        observe(tracker, B if i % 2 == 0 else A, end=6.4 + i * 1.6)
    assert tracker.primary == "speaker_1"


def test_ambiguous_voice_and_gap_do_not_update_or_establish_reference():
    tracker = SpeakerTracker()
    observe(tracker)
    observe(tracker, B, 3.2)
    mixed = observe(tracker, A + B, 4.8)
    assert mixed["reason"] == "ambiguous_similarity"
    assert tracker.voices["speaker_1"].windows == 1
    assert tracker.primary is None
    observe(tracker, end=6.4)
    tracker.break_sequence()
    observe(tracker, end=8)
    assert tracker.primary is None


def test_bot_audio_cannot_enroll_or_switch_or_drift_reference():
    tracker = SpeakerTracker()
    for i in range(6):
        observe(tracker, B, end=i * 1.6, learn=False)
    assert tracker.voices == {}
    for end in [1.6, 3.2, 4.8]:
        observe(tracker, end=end)
    original = tracker.voices["speaker_1"].centroid.copy()
    for i in range(6):
        observe(tracker, B, end=6.4 + i * 1.6, learn=False)
    assert tracker.primary == "speaker_1"
    np.testing.assert_array_equal(tracker.voices["speaker_1"].centroid, original)


def test_invalid_embedding_is_rejected():
    tracker = SpeakerTracker()
    for vector in [np.zeros(3), np.array([float("nan"), 0, 0])]:
        assert observe(tracker, vector)["reason"] == "invalid_embedding"
    assert not tracker.voices


def test_consistent_novel_voice_can_leave_gray_band_without_relaxing_match_threshold():
    tracker = SpeakerTracker()
    for end in [1.6, 3.2, 4.8]:
        observe(tracker, end=end)
    gray = np.array([0.45, np.sqrt(1 - 0.45**2), 0], dtype=np.float32)
    first = observe(tracker, gray, 6.4)
    assert first["reason"] == "novel_voice_pending"
    assert len(tracker.voices) == 1
    assert observe(tracker, gray, 8)["action"] == "uncertain"
    assert tracker.primary == "speaker_1"
    changed = observe(tracker, gray, 9.6)
    assert changed["action"] == "primary_changed"
    assert changed["reason"] == "consistent_novel_voice"
    assert changed["previous_best_similarity"] == 0.45
    assert observe(tracker, gray, 11.2)["action"] == "primary_match"


def test_inconsistent_gray_band_and_intervening_primary_do_not_promote():
    tracker = SpeakerTracker()
    for end in [1.6, 3.2, 4.8]:
        observe(tracker, end=end)
    gray_b = np.array([0.45, np.sqrt(1 - 0.45**2), 0], dtype=np.float32)
    gray_c = np.array([0.45, 0, np.sqrt(1 - 0.45**2)], dtype=np.float32)
    for i in range(10):
        observe(tracker, gray_b if i % 2 else gray_c, 6.4 + i * 1.6)
    assert len(tracker.voices) == 1
    observe(tracker, gray_b, 24)
    observe(tracker, A, 25.6)
    assert observe(tracker, gray_b, 27.2)["candidate_windows"] == 1
    tracker.break_sequence()
    assert observe(tracker, gray_b, 28.8)["candidate_windows"] == 1
    assert tracker.primary == "speaker_1"


@pytest.mark.parametrize("rate", [8000, 16000])
def test_analyzer_uses_disjoint_windows_and_resets_pcm_at_gap(monkeypatch, rate):
    import api.services.speaker_tracking.analyzer as analyzer_module

    analyzer = CallAnalyzer.__new__(CallAnalyzer)
    analyzer.vad = Mock(return_value=np.array([1.0]))
    analyzer.tracker = SpeakerTracker()
    analyzer._format = None
    analyzer.reset()
    embedded = []

    def embed(samples, sample_rate):
        embedded.append((samples.copy(), sample_rate))
        return A

    monkeypatch.setattr(analyzer_module, "embed", embed)
    pcm = np.full(int(rate * 1.6), 1000, dtype="<i2").tobytes()
    # A partial pre-gap voice must not be stitched to the next fragment.
    analyzer.process(pcm[:rate], rate, 0.5, True, False)
    result = analyzer.process(pcm, rate, 3.6, True, True)
    assert len(result) == 1
    assert embedded[0][0].size == int(rate * 1.6)
    assert result[0]["audio_start_seconds"] == 2.0
    assert result[0]["action"] == "candidate"
    assert analyzer.flush(rate, 3.6, True) == []


class FakeAnalyzer:
    def __init__(self):
        self.processed = []

    def process(self, *args):
        self.processed.append(args)
        return [{"action": "candidate", "speaker": "speaker_1"}]

    def flush(self, *args):
        return []


def frame_event(source, frame, direction=FrameDirection.DOWNSTREAM):
    return FramePushed(
        source=source,
        destination=object(),
        frame=frame,
        direction=direction,
        timestamp=0,
    )


def audio():
    return InputAudioRawFrame(audio=bytes(640), sample_rate=16000, num_channels=1)


@pytest.fixture
def shadow(monkeypatch):
    analyzer = FakeAnalyzer()
    monkeypatch.setattr(module, "CallAnalyzer", lambda: analyzer)

    def immediate(function, *args):
        future = asyncio.get_running_loop().create_future()
        try:
            future.set_result(function(*args))
        except Exception as exc:  # noqa: BLE001 - emulate an executor future
            future.set_exception(exc)
        return future

    monkeypatch.setattr(module, "submit_native", immediate)
    logs = InMemoryLogsBuffer(workflow_run_id=1)
    source = object()
    observer = module.SpeakerTrackingObserver(input_processor=source, logs_buffer=logs)
    return observer, source, logs, analyzer


@pytest.mark.asyncio
async def test_shadow_only_observes_inbound_once_and_preserves_frames(shadow):
    observer, source, logs, analyzer = shadow
    frame = audio()
    before = frame.audio
    await observer.on_push_frame(frame_event(source, frame))
    await observer.on_push_frame(frame_event(object(), frame))
    await observer.on_push_frame(frame_event(source, audio(), FrameDirection.UPSTREAM))
    text = TranscriptionFrame(text="hello", user_id="caller", timestamp="now")
    interrupt = InterruptionFrame()
    await observer.on_push_frame(frame_event(source, text))
    await observer.on_push_frame(frame_event(source, interrupt))
    await observer.finish()
    assert len(analyzer.processed) == 1
    assert analyzer.processed[0][0] == before == frame.audio
    assert text.text == "hello"
    events = logs.get_events()
    assert [e["payload"]["action"] for e in events] == [
        "started",
        "ready",
        "candidate",
        "summary",
    ]
    assert all(e["type"] == "rtf-speaker-tracking" for e in events)
    assert observer._analyzer is None
    await observer.finish()
    assert logs.get_events() == events


@pytest.mark.asyncio
async def test_supervisor_suppresses_learning_and_bot_speech_is_tagged(shadow):
    observer, source, logs, analyzer = shadow
    observer._supervisor = SimpleNamespace(blocks_workflow=True)
    await observer.on_push_frame(frame_event(source, audio()))
    assert not observer._queue
    observer._supervisor.blocks_workflow = False
    await observer.on_push_frame(frame_event(source, BotStartedSpeakingFrame()))
    await observer.on_push_frame(frame_event(source, audio()))
    await observer.on_push_frame(frame_event(source, BotStoppedSpeakingFrame()))
    await observer.on_push_frame(frame_event(source, audio()))
    await observer.finish()
    assert [args[3] for args in analyzer.processed] == [False, True]
    assert logs.get_events()[-1]["payload"]["counts"]["answer_supervisor_pending"] == 1


@pytest.mark.asyncio
async def test_model_failure_is_recorded_without_throwing(shadow, monkeypatch):
    observer, source, logs, _ = shadow

    def fail():
        raise FileNotFoundError("missing model")

    monkeypatch.setattr(module, "CallAnalyzer", fail)
    await observer.on_push_frame(frame_event(source, audio()))
    await observer.finish()
    assert any(
        e["payload"].get("reason") == "FileNotFoundError" for e in logs.get_events()
    )
    assert observer._analyzer is None


@pytest.mark.asyncio
async def test_overload_bounds_queue_and_reports_drops(shadow):
    observer, source, logs, _ = shadow
    for _ in range(500):
        await observer.on_push_frame(frame_event(source, audio()))
    assert observer._queued_seconds <= observer.MAX_QUEUE_SECONDS
    assert len(observer._queue) <= 150
    await observer.finish()
    assert logs.get_events()[-1]["payload"]["counts"]["queue_overflow"] > 0


@pytest.mark.asyncio
async def test_slow_native_work_does_not_block_capture_or_unbounded_finish(
    shadow, monkeypatch
):
    observer, source, logs, _ = shadow
    pending = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(module, "submit_native", lambda *args: pending)
    await observer.on_push_frame(frame_event(source, audio()))
    await asyncio.sleep(0)
    await asyncio.wait_for(
        observer.on_push_frame(frame_event(source, audio())), timeout=0.1
    )
    await asyncio.wait_for(observer.finish(), timeout=1.0)
    assert logs.get_events()[-1]["payload"]["counts"]["finish_timeout"] == 1


@pytest.mark.asyncio
async def test_cancelled_diagnostic_worker_does_not_cancel_call_completion(shadow):
    observer, source, logs, _ = shadow
    await observer.on_push_frame(frame_event(source, audio()))
    observer._worker.cancel()
    await asyncio.gather(observer._worker, return_exceptions=True)
    await observer.finish()
    assert logs.get_events()[-1]["payload"]["counts"]["worker_cancelled"] == 1
    assert observer._analyzer is None


def test_setting_defaults_off_and_round_trips():
    assert not WorkflowConfigurationDefaults().speaker_tracking_enabled
    assert not WorkflowConfigurationDefaults.model_validate(
        {"speaker_tracking_enabled": None}
    ).speaker_tracking_enabled
    assert (
        WorkflowConfigurationDefaults(speaker_tracking_enabled=True).model_dump()[
            "speaker_tracking_enabled"
        ]
        is True
    )


@pytest.mark.asyncio
async def test_cancelled_native_await_keeps_its_capacity_until_thread_finishes():
    release = threading.Event()
    started = [threading.Event(), threading.Event()]

    def blocked(index):
        started[index].set()
        release.wait(timeout=2)

    jobs = [module.submit_native(blocked, index) for index in range(2)]
    try:
        for event in started:
            assert await asyncio.to_thread(event.wait, 1)
        jobs[0].cancel()
        await asyncio.sleep(0)
        assert module.submit_native(lambda: None) is None
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)


@pytest.mark.asyncio
async def test_log_limit_keeps_summary_but_never_adds_transcript_content(shadow):
    observer, _, logs, _ = shadow
    observer.MAX_EVENTS = 2
    for _ in range(10):
        await observer._record({"action": "uncertain"})
    await observer.finish()
    assert len(logs.get_events()) == 3
    assert logs.get_events()[-1]["payload"]["counts"]["uncertain"] == 10
    assert not logs.generate_transcript_text()


@pytest.mark.asyncio
async def test_real_pipeline_forwards_audio_text_and_interrupt_while_shadow_is_stuck(
    monkeypatch,
):
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker
    from pipecat.processors.frame_processor import FrameProcessor

    from api.services.pipecat.worker_runner import (
        run_pipeline_worker,
        wait_for_pipeline_worker_started,
    )

    received = asyncio.Queue()

    class Passthrough(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    class Sink(Passthrough):
        async def process_frame(self, frame, direction):
            if isinstance(
                frame, (InputAudioRawFrame, TranscriptionFrame, InterruptionFrame)
            ):
                received.put_nowait(frame)
            await super().process_frame(frame, direction)

    source = Passthrough()
    logs = InMemoryLogsBuffer(workflow_run_id=1)
    observer = module.SpeakerTrackingObserver(input_processor=source, logs_buffer=logs)
    pending = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(module, "submit_native", lambda *args: pending)
    worker = PipelineWorker(Pipeline([source, Sink()]), enable_rtvi=False)
    worker.add_observer(observer)
    running = asyncio.create_task(run_pipeline_worker(worker))
    try:
        await wait_for_pipeline_worker_started(worker, run_task=running)
        frames = [
            audio(),
            TranscriptionFrame(text="caller", user_id="1", timestamp="now"),
            InterruptionFrame(),
        ]
        for frame in frames:
            await worker.queue_frame(frame)
            assert await asyncio.wait_for(received.get(), 1) is frame
        assert not pending.done()
    finally:
        await worker.cancel()
        await asyncio.wait_for(running, 3)
        await observer.finish()

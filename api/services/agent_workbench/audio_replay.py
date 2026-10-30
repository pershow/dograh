"""Bounded PCM replay through the production speech pipeline, with private audio.

This measures the server pipeline. It does not emulate SIP, codecs, browser
devices, network jitter or a human hearing the output.
"""

import asyncio
import hashlib
import io
import re
import tempfile
import time
import uuid
import wave
from pathlib import Path

from pipecat.frames.frames import InputAudioRawFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from api.db import db_client
from api.services.agent_workbench.errors import WorkbenchError
from api.services.pipecat.worker_runner import run_worker_runner


def decode_wav(data):
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            rate = source.getframerate()
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or rate not in (8000, 16000)
                or source.getcomptype() != "NONE"
            ):
                raise WorkbenchError(
                    "语音回放需要 8 kHz 或 16 kHz、单声道、16 位 PCM WAV。"
                )
            if not 0 < source.getnframes() <= rate * 180:
                raise WorkbenchError("WAV 时长必须在 0 到 180 秒之间。")
            pcm = source.readframes(source.getnframes())
            if len(pcm) != source.getnframes() * 2:
                raise WorkbenchError("WAV 音频数据不完整。")
            return pcm, rate
    except (wave.Error, EOFError) as error:
        raise WorkbenchError("无法读取 PCM WAV 音频。") from error


def encode_wav(pcm, rate):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)
    return stream.getvalue()


def make_timeline(spec, decoded):
    rate = 8000 if spec.channel == "phone" else 16000
    timeline = bytearray(spec.duration_ms * rate // 1000 * 2)
    occupied = []
    receipts = []
    for clip, (pcm, clip_rate) in zip(spec.clips, decoded, strict=True):
        if clip_rate != rate:
            raise WorkbenchError(
                f"此回放通道需要 {rate} Hz 音频；请显式转换采样率后上传。"
            )
        start = clip.offset_ms * rate // 1000 * 2
        end = start + len(pcm)
        if end > len(timeline) or any(start < b and end > a for a, b in occupied):
            raise WorkbenchError("音频片段不能重叠或超出回放时长。")
        occupied.append((start, end))
        timeline[start:end] = pcm
        receipts.append(
            {
                "asset_id": clip.asset_id,
                "offset_ms": clip.offset_ms,
                "duration_ms": len(pcm) * 500 / rate,
                "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
            }
        )
    return bytes(timeline), rate, receipts


def audio_assertions(case, transcript, evidence):
    normalize = lambda text: re.sub(r"[\W_]+", "", text).casefold()
    heard = normalize(" ".join(m["text"] for m in transcript if m["role"] == "user"))
    offset, matches = 0, []
    for expected in case.user_messages:
        normalized = normalize(expected)
        found = heard.find(normalized, offset) if normalized else -1
        matches.append(found >= 0)
        if found >= 0:
            offset = found + len(normalized)
    return [
        {
            "kind": "audio_inputs_submitted",
            "value": 1,
            "actual": evidence["input_coverage"],
            "passed": evidence["input_coverage"] == 1,
        },
        {
            "kind": "asr_expected_sequence",
            "value": case.user_messages,
            "actual": [m["text"] for m in transcript if m["role"] == "user"],
            "passed": all(matches),
        },
    ]


class ReplayInput(BaseInputTransport):
    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)


class ReplayOutput(BaseOutputTransport):
    def __init__(self, params, replay):
        super().__init__(params)
        self.replay = replay

    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame):
        if len(self.replay.output_pcm) + len(frame.audio) > self.replay.rate * 2 * 200:
            raise RuntimeError("Audio replay output exceeded its bound")
        self.replay.output_pcm.extend(frame.audio)
        self.replay.output_frames += 1
        if self.replay.first_output_ms is None:
            self.replay.first_output_ms = self.replay.elapsed_ms()
        # Match a paced speaker sink; never drain synthesized minutes of audio
        # instantaneously and report the queue time as heard latency.
        await asyncio.sleep(
            len(frame.audio) / (frame.sample_rate * frame.num_channels * 2)
        )
        return True


class ReplayTransport(BaseTransport):
    def __init__(self, replay):
        super().__init__()
        params = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=replay.rate,
            audio_out_sample_rate=replay.rate,
            audio_out_auto_silence=False,
            audio_out_end_silence_secs=0,
        )
        self._input = ReplayInput(params)
        self._output = ReplayOutput(params, replay)

    def input(self):
        return self._input

    def output(self):
        return self._output


class AudioReplaySession:
    def __init__(self, spec, decoded):
        self.spec = spec
        self.pcm, self.rate, self.inputs = make_timeline(spec, decoded)
        self.transport = ReplayTransport(self)
        self.started = None
        self.output_pcm = bytearray()
        self.output_frames = 0
        self.first_output_ms = None
        self.sent_bytes = 0
        self.max_input_lag_ms = 0
        self.transcript = []
        self.player = None
        self.timeline_finished = False

    def elapsed_ms(self):
        return (
            round((time.monotonic() - self.started) * 1000, 2) if self.started else None
        )

    async def run(
        self,
        *,
        engine,
        task,
        runner,
        user_config,
        user_aggregator,
        assistant_aggregator,
        metrics,
        call_events,
        termination_funnel,
    ):
        policy = engine.evaluation_policy
        original_greeting_logger = engine.greeting.log_speech

        async def greeting_message(speech):
            # Direct greetings bypass the assistant context aggregator. Use
            # playback delivery, rather than the requested/possibly cut text.
            if speech.manage_text and speech.delivered_text:
                self.transcript.append(
                    {
                        "role": "assistant",
                        "text": speech.delivered_text,
                        "at_ms": self.elapsed_ms(),
                        "interrupted": speech.outcome.value == "interrupted",
                        "speech_id": speech.id,
                    }
                )
            if original_greeting_logger:
                await original_greeting_logger(speech)

        engine.greeting.log_speech = greeting_message

        @user_aggregator.event_handler("on_user_turn_message_added")
        async def user_message(_source, message):
            self.transcript.append(
                {
                    "role": "user",
                    "text": message.content or "",
                    "at_ms": self.elapsed_ms(),
                }
            )

        @assistant_aggregator.event_handler("on_assistant_turn_stopped")
        async def assistant_message(_source, message):
            self.transcript.append(
                {
                    "role": "assistant",
                    "text": message.content or "",
                    "at_ms": self.elapsed_ms(),
                    "interrupted": message.interrupted,
                }
            )

        @task.event_handler("on_pipeline_error")
        async def pipeline_error(_source, _frame):
            policy.errors.append("Audio runtime reported a pipeline error")

        async def terminate(reason, _frame=None):
            policy.errors.append("Audio pipeline terminated: " + str(reason))
            await engine.end_call_with_reason(
                "audio_evaluation_error", abort_immediately=True
            )

        termination_funnel.set_termination_handler(terminate)

        async def play():
            try:
                engine.opening.deadline.arm()
                engine.finalize_initial_agent(user_config)
                engine.begin_initial_agent_startup()
                if not await engine.wait_for_generation_ready():
                    raise WorkbenchError("Audio Agent did not become ready")
                engine.call_monitor.activate()
                self.started = time.monotonic()
                opening = await engine.queue_node_opening(
                    node_id=engine.active_agent.workflow.start_node_id,
                    previous_node_id=None,
                    generate_if_no_greeting=True,
                )
                if engine.opening.input_gate is not None:
                    engine.opening.prepare(
                        engine.release_opening_input(opening.playback)
                    )
                chunk_bytes = self.rate * 2 // 50
                for offset in range(0, len(self.pcm), chunk_bytes):
                    if task.has_finished() or engine.is_call_disposed():
                        break
                    due = self.started + offset / (self.rate * 2)
                    await asyncio.sleep(max(0, due - time.monotonic()))
                    self.max_input_lag_ms = max(
                        self.max_input_lag_ms, max(0, (time.monotonic() - due) * 1000)
                    )
                    block = self.pcm[offset : offset + chunk_bytes]
                    await self.transport.input().push_audio_frame(
                        InputAudioRawFrame(block, self.rate, 1)
                    )
                    self.sent_bytes += len(block)
                if not task.has_finished() and not engine.is_call_disposed():
                    self.timeline_finished = True
                    await engine.end_call_with_reason("audio_replay_finished")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                policy.errors.append(type(error).__name__ + ": " + str(error)[:500])
                await engine.end_call_with_reason(
                    "audio_replay_failed", abort_immediately=True
                )

        @task.event_handler("on_pipeline_started")
        async def started(_source, _frame):
            self.player = asyncio.create_task(play(), name="workbench-audio-replay")

        try:
            async with asyncio.timeout(self.spec.duration_ms / 1000 + 45):
                await run_worker_runner(runner, task)
        finally:
            if self.player is not None:
                self.player.cancel()
                await asyncio.gather(self.player, return_exceptions=True)
            await engine.retire_agents("audio_replay_cleanup", drain=False)
            await engine.greeting.flush()
            engine.greeting.log_speech = original_greeting_logger
            if not task.has_finished():
                await task.cancel(reason="audio_replay_cleanup")
            if call_events:
                await call_events.finish()
        gathered = await engine.get_gathered_context()
        return {
            "gathered_context": gathered,
            "transcript": self.transcript,
            "usage": metrics.get_all_usage_metrics_serialized(),
            "completed": engine.is_call_disposed()
            and not self.timeline_finished
            and not policy.errors,
            "audio_evidence": {
                "measurement_boundary": "server_pipeline_replay",
                "channel": self.spec.channel,
                "sample_rate": self.rate,
                "input_clips": self.inputs,
                "input_coverage": self.sent_bytes / len(self.pcm),
                "max_input_lag_ms": round(self.max_input_lag_ms, 2),
                "output_frames": self.output_frames,
                "first_output_from_replay_start_ms": self.first_output_ms,
                "errors": list(policy.errors),
            },
        }


async def read_audio_asset(org, session_id, asset_id):
    from api.services.agent_workbench.attachments import MAX_BYTES, PrivateAssetStore

    asset = await db_client.wb_get(org, session_id, "assets", asset_id)
    if (
        asset["status"] != "ready"
        or asset["parsed"].get("kind") != "audio"
        or asset["byte_size"] > MAX_BYTES
    ):
        raise WorkbenchError("音频附件尚未就绪或超出大小限制。")
    with tempfile.TemporaryDirectory(prefix="workbench-audio-") as directory:
        local = Path(directory) / "audio.wav"
        await PrivateAssetStore().download(asset["object_key"], local)
        if local.stat().st_size > MAX_BYTES:
            raise WorkbenchError("Audio attachment exceeded size limit")
        data = local.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset["sha256"]:
            raise WorkbenchError("Audio attachment integrity check failed")
        return data


async def replay_case(org, session_id, workflow_id, run_id, policy):
    from api.services.agent_workbench.attachments import PrivateAssetStore
    from api.services.pipecat.audio_config import AudioConfig
    from api.services.pipecat.run_pipeline import _run_pipeline_impl

    decoded = []
    for clip in policy.case.audio.clips:
        decoded.append(
            decode_wav(await read_audio_asset(org, session_id, clip.asset_id))
        )
    replay = AudioReplaySession(policy.case.audio, decoded)
    result = await _run_pipeline_impl(
        replay.transport,
        workflow_id,
        run_id,
        user_id=0,
        organization_id=org,
        audio_config=AudioConfig(replay.rate, replay.rate, replay.rate),
        evaluation_policy=policy,
        evaluation_replay=replay,
    )
    if replay.output_pcm:
        audio = encode_wav(bytes(replay.output_pcm), replay.rate)
        key = f"organizations/{org}/workbench/{session_id}/{uuid.uuid4()}"
        await PrivateAssetStore().put(key, audio, "audio/wav")
        asset = await db_client.wb_asset(
            org,
            session_id,
            name=f"replay-{run_id}-agent.wav",
            content_type="audio/wav",
            byte_size=len(audio),
            sha256=hashlib.sha256(audio).hexdigest(),
            object_key=key,
        )
        await db_client.wb_asset_result(
            org,
            session_id,
            asset["id"],
            parsed={
                "kind": "audio",
                "sample_rate": replay.rate,
                "channels": 1,
                "duration_ms": len(replay.output_pcm) * 500 / replay.rate,
            },
        )
        result["audio_evidence"]["output_asset_id"] = asset["id"]
    return result

import copy
from unittest.mock import AsyncMock, Mock, patch
import pytest

from pipecat.frames.frames import InputAudioRawFrame, TranscriptionFrame
from pipecat.tests import MockLLMService, MockTTSService

from api.schemas.agent_workbench import SimulationCase
from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.agent_workbench.audio_replay import AudioReplaySession
from api.services.agent_workbench.evaluation_policy import EvaluationPolicy
from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.run_pipeline import _run_pipeline_impl
from api.tests.integrations._run_pipeline_helpers import (
    USER_CONFIGURATION,
    PassthroughProcessor,
    create_workflow_run_rows,
    patch_run_pipeline_externals,
)
from api.tests.integrations.test_run_pipeline_text_greeting import WORKFLOW_DEFINITION


class Recognizer(PassthroughProcessor):
    def __init__(self):
        super().__init__()
        self.heard = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if (
            isinstance(frame, InputAudioRawFrame)
            and any(frame.audio)
            and not self.heard
        ):
            self.heard = True
            await self.push_frame(
                TranscriptionFrame("你好", "replay", "", finalized=True)
            )


@pytest.mark.parametrize("channel,rate", [("browser", 16000), ("phone", 8000)])
async def test_audio_replay_uses_real_pipeline_without_normal_completion_effects(
    db_session, async_session, monkeypatch, channel, rate
):
    graph = copy.deepcopy(WORKFLOW_DEFINITION)
    graph["nodes"][0]["data"]["greeting"] = "您好"
    graph["nodes"][0]["data"]["allow_interrupt"] = True
    workflow_run, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=graph,
        name_prefix="Audio replay",
        provider_id_suffix="audio-replay",
    )
    workflow_run.purpose = "evaluation"
    workflow_run.mode = "audio_replay"
    from api.db.models import WorkflowDefinitionModel

    definition = await async_session.get(
        WorkflowDefinitionModel, workflow_run.definition_id
    )
    definition.workflow_configurations = {
        "agent_behavior": {"enabled": True, "goal": "Help the user"},
        "runtime_evidence_enabled": True,
    }
    await async_session.flush()
    monkeypatch.setattr("api.constants.AUTH_PROVIDER", "local")
    case = SimulationCase(
        name="audio",
        user_messages=["你好"],
        assertions=[{"kind": "contains", "value": "您好"}],
        audio={
            "channel": channel,
            "clips": [{"asset_id": "test", "offset_ms": 1500}],
            "duration_ms": 8000,
        },
    )
    policy = EvaluationPolicy(
        case, EffectiveAIModelConfiguration.model_validate(USER_CONFIGURATION)
    )
    replay = AudioReplaySession(case.audio, [(b"\x10\x00" * (rate // 10), rate)])
    stt = Recognizer()
    notify, integrations, export = AsyncMock(), Mock(), Mock()
    with (
        patch_run_pipeline_externals(
            [],
            llm=MockLLMService(
                mock_steps=[MockLLMService.create_text_chunks("我可以帮助您")],
                chunk_delay=0.001,
            ),
            tts=MockTTSService(mock_audio_duration_ms=50, frame_delay=0),
        ),
        patch("api.services.pipecat.run_pipeline.create_stt_service", return_value=stt),
        patch(
            "api.services.pipecat.event_handlers.notify_campaign_call_completed", notify
        ),
        patch(
            "api.services.pipecat.run_pipeline.create_runtime_sessions", integrations
        ),
        patch("api.services.observability.call_events.delivery.submit", export),
    ):
        result = await _run_pipeline_impl(
            replay.transport,
            workflow.id,
            workflow_run.id,
            user.id,
            organization_id=workflow.organization_id,
            audio_config=AudioConfig(rate, rate, rate),
            evaluation_policy=policy,
            evaluation_replay=replay,
        )
    assert stt.heard
    assert result["audio_evidence"]["input_coverage"] == 1
    assert result["audio_evidence"]["output_frames"] > 0
    assert any(
        m["role"] == "assistant" and m["text"] == "您好" for m in result["transcript"]
    ), result["transcript"]
    assert any(
        m["role"] == "user" and m["text"] == "你好" for m in result["transcript"]
    ), result["transcript"]
    assert any(
        m["role"] == "assistant" and "帮助" in m["text"] for m in result["transcript"]
    ), result["transcript"]
    assert result["gathered_context"]["runtime_evidence"]["events"]
    assert result["gathered_context"]["task_state"]["last_user_text"] == "你好"
    assert result["gathered_context"]["task_state"]["turn"] == 1
    notify.assert_not_called()
    integrations.assert_not_called()
    export.assert_not_called()

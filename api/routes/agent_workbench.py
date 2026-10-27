"""Opt-in workbench API. Every child lookup is rooted in the authenticated org."""

import asyncio
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    WebSocket,
)
from fastapi.routing import APIRoute

from api.db import db_client
from api.db.models import UserModel
from api.schemas.agent_workbench import (
    BuildModelSettings,
    BuildModelSettingsResponse,
    CapabilityConfiguration,
    ConfirmMemory,
    CreateSession,
    EvaluateRequest,
    Instruction,
    RevokeMemory,
    SessionSettings,
    SimulationSuite,
    TuneRequest,
    VoiceToolRequest,
    WorkbenchResponse,
)
from api.services.agent_workbench.attachments import parse_attachment, upload_attachment
from api.services.agent_workbench.config import WorkbenchSettings
from api.services.agent_workbench.context import list_agent_runs
from api.services.agent_workbench.dependencies import (
    BUILD_LLM_PROVIDERS,
    dependency_report,
    get_build_model,
    save_build_model,
)
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.jobs import enqueue
from api.services.agent_workbench.voice_commands import voice_command
from api.services.auth.depends import get_user_with_selected_organization, get_user_ws
from api.services.configuration.masking import (
    mask_workflow_configurations,
    mask_workflow_definition,
)


class WorkbenchRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                return await handler(request)
            except WorkbenchError as error:
                raise HTTPException(
                    status_code=error.status_code, detail=str(error)
                ) from error

        return wrapped


router = APIRouter(
    prefix="/agent-workbench", tags=["agent-workbench"], route_class=WorkbenchRoute
)
User = Annotated[UserModel, Depends(get_user_with_selected_organization)]


def require_enabled():
    if not WorkbenchSettings.load().enabled:
        raise HTTPException(status_code=404, detail="Agent workbench is not enabled")


Enabled = Annotated[None, Depends(require_enabled)]


def public_data(data):
    # Mask legacy node secrets and runtime configuration before returning snapshots.
    for key in ("target", "snapshot"):
        if key in data:
            data[key]["workflow_json"] = mask_workflow_definition(
                data[key]["workflow_json"]
            )
            data[key]["workflow_configurations"] = mask_workflow_configurations(
                data[key]["workflow_configurations"]
            )
    for asset in data.get("assets", []):
        asset.pop("object_key", None)
        asset.pop("parsed", None)
    for job in data.get("jobs", []):
        job.pop("lease_token", None)
    data.pop("lease_token", None)
    if isinstance(data.get("parsed"), dict):
        data["parsed"].pop("model_image", None)
    return WorkbenchResponse(data=data)


@router.get("/capabilities", response_model=WorkbenchResponse)
async def workbench_capabilities(user: User):
    return WorkbenchResponse(
        data=await asyncio.to_thread(WorkbenchSettings.load().capabilities)
    )


@router.get(
    "/build-model",
    response_model=BuildModelSettingsResponse,
    operation_id="get_workbench_build_model",
)
async def workbench_build_model(user: User, _: Enabled):
    return BuildModelSettingsResponse(
        llm=await get_build_model(user.selected_organization_id),
        supported_providers=list(BUILD_LLM_PROVIDERS),
    )


@router.put(
    "/build-model",
    response_model=BuildModelSettingsResponse,
    operation_id="update_workbench_build_model",
)
async def workbench_update_build_model(
    body: BuildModelSettings, user: User, _: Enabled
):
    return BuildModelSettingsResponse(
        llm=await save_build_model(user.selected_organization_id, body.llm),
        supported_providers=list(BUILD_LLM_PROVIDERS),
    )


@router.post("/sessions", response_model=WorkbenchResponse)
async def workbench_create_session(body: CreateSession, user: User, _: Enabled):
    return public_data(
        await db_client.wb_create_session(
            user.selected_organization_id, user.id, **body.model_dump()
        )
    )


@router.get("/sessions", response_model=WorkbenchResponse)
async def workbench_sessions(workflow_id: int, user: User, _: Enabled):
    return public_data(
        {
            "sessions": await db_client.wb_sessions(
                user.selected_organization_id, user.id, workflow_id
            )
        }
    )


@router.get("/sessions/{session_id}", response_model=WorkbenchResponse)
async def workbench_session(
    session_id: str, user: User, _: Enabled, if_revision: str | None = None
):
    return public_data(
        await db_client.wb_context(
            user.selected_organization_id, session_id, if_revision=if_revision
        )
    )


@router.get("/sessions/{session_id}/agent-runs", response_model=WorkbenchResponse)
async def workbench_agent_runs(
    session_id: str, user: User, _: Enabled, limit: int = Query(default=20, ge=1, le=20)
):
    return WorkbenchResponse(
        data=await list_agent_runs(user.selected_organization_id, session_id, limit)
    )


@router.patch("/sessions/{session_id}", response_model=WorkbenchResponse)
async def workbench_settings(
    session_id: str, body: SessionSettings, user: User, _: Enabled
):
    return public_data(
        await db_client.wb_settings(
            user.selected_organization_id, session_id, body.model_protocol
        )
    )


@router.get("/sessions/{session_id}/dependencies", response_model=WorkbenchResponse)
async def workbench_dependencies(
    session_id: str,
    user: User,
    _: Enabled,
    candidate_id: str | None = None,
    purpose: Literal["build", "evaluate", "tune", "voice"] = "build",
):
    org = user.selected_organization_id
    context = await db_client.wb_context(org, session_id)
    target = context["target"]
    if candidate_id:
        target = (await db_client.wb_get(org, session_id, "candidates", candidate_id))[
            "snapshot"
        ]
    return WorkbenchResponse(data=await dependency_report(org, target, purpose))


@router.post(
    "/sessions/{session_id}/capability-candidates",
    response_model=WorkbenchResponse,
    operation_id="create_workbench_capability_candidate",
)
async def workbench_capability_candidate(
    session_id: str, body: CapabilityConfiguration, user: User, _: Enabled
):
    return public_data(
        await db_client.wb_capability_candidate(
            user.selected_organization_id,
            session_id,
            expected_fingerprint=body.expected_fingerprint,
            candidate_id=body.candidate_id,
            patch=body.model_dump(
                mode="json", exclude={"expected_fingerprint", "candidate_id"}
            ),
        )
    )


@router.get("/sessions/{session_id}/events", response_model=WorkbenchResponse)
async def workbench_events(
    session_id: str,
    user: User,
    _: Enabled,
    after: int | None = Query(None, ge=0),
    before: int | None = Query(None, ge=1),
    limit: int = Query(60, ge=1, le=200),
):
    return WorkbenchResponse(
        data=await db_client.wb_event_page(
            user.selected_organization_id,
            session_id,
            after=after,
            before=before,
            limit=limit,
        )
    )


@router.post("/sessions/{session_id}/instructions", response_model=WorkbenchResponse)
async def workbench_instruction(
    session_id: str, body: Instruction, user: User, _: Enabled
):
    return public_data(
        await enqueue(
            user.selected_organization_id,
            session_id,
            "build",
            body.model_dump(exclude={"idempotency_key"}),
            body.idempotency_key,
        )
    )


@router.get(
    "/sessions/{session_id}/jobs/{job_id}/basis", response_model=WorkbenchResponse
)
async def workbench_task_basis(
    session_id: str,
    job_id: str,
    user: User,
    _: Enabled,
    source_id: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=12000, ge=1, le=24000),
):
    from api.services.agent_workbench.basis import source_page

    basis = await db_client.wb_task_basis(
        user.selected_organization_id, session_id, job_id
    )
    return WorkbenchResponse(data=source_page(basis, source_id, offset, limit))


@router.get("/sessions/{session_id}/memory", response_model=WorkbenchResponse)
async def workbench_memory(
    session_id: str, user: User, _: Enabled, include_history: bool = False
):
    return WorkbenchResponse(
        data=await db_client.wb_memory(
            user.selected_organization_id, session_id, include_history=include_history
        )
    )


@router.post("/sessions/{session_id}/memory", response_model=WorkbenchResponse)
async def workbench_confirm_memory(
    session_id: str, body: ConfirmMemory, user: User, _: Enabled
):
    return WorkbenchResponse(
        data=await db_client.wb_confirm_memory(
            user.selected_organization_id, session_id, user.id, **body.model_dump()
        )
    )


@router.post(
    "/sessions/{session_id}/memory/{memory_id}/revoke", response_model=WorkbenchResponse
)
async def workbench_revoke_memory(
    session_id: str, memory_id: str, body: RevokeMemory, user: User, _: Enabled
):
    return WorkbenchResponse(
        data=await db_client.wb_revoke_memory(
            user.selected_organization_id,
            session_id,
            user.id,
            memory_id,
            body.expected_revision,
        )
    )


@router.get("/sessions/{session_id}/memory-sources", response_model=WorkbenchResponse)
async def workbench_memory_sources(
    session_id: str,
    user: User,
    _: Enabled,
    source_type: Literal["request", "attachment"] = "request",
    query: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(40, ge=1, le=60),
):
    return WorkbenchResponse(
        data=await db_client.wb_memory_sources(
            user.selected_organization_id,
            session_id,
            source_type=source_type,
            query=query,
            offset=offset,
            limit=limit,
        )
    )


@router.get(
    "/sessions/{session_id}/memory-sources/{source_id}",
    response_model=WorkbenchResponse,
)
async def workbench_memory_source(
    session_id: str,
    source_id: str,
    user: User,
    _: Enabled,
    source_type: Literal["request", "attachment"] = "request",
):
    return WorkbenchResponse(
        data=await db_client.wb_memory_source(
            user.selected_organization_id, session_id, source_type, source_id
        )
    )


@router.post("/sessions/{session_id}/assets", response_model=WorkbenchResponse)
async def workbench_upload(
    session_id: str, user: User, _: Enabled, file: Annotated[UploadFile, File()]
):
    asset = await upload_attachment(user.selected_organization_id, session_id, file)
    asset.pop("object_key", None)
    return public_data(asset)


@router.get(
    "/sessions/{session_id}/assets/{asset_id}/audio",
    operation_id="download_workbench_audio",
    responses={
        200: {
            "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}
        }
    },
)
async def download_workbench_audio(
    session_id: str, asset_id: str, user: User, _: Enabled
) -> Response:
    from api.services.agent_workbench.audio_replay import read_audio_asset

    audio = await read_audio_asset(user.selected_organization_id, session_id, asset_id)
    return Response(
        audio,
        media_type="audio/wav",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "attachment; filename=workbench-audio.wav",
        },
    )


@router.post(
    "/sessions/{session_id}/assets/{asset_id}/retry", response_model=WorkbenchResponse
)
async def workbench_retry_asset(session_id: str, asset_id: str, user: User, _: Enabled):
    asset = await parse_attachment(user.selected_organization_id, session_id, asset_id)
    asset.pop("object_key", None)
    return public_data(asset)


@router.get(
    "/sessions/{session_id}/candidates/{candidate_id}", response_model=WorkbenchResponse
)
async def workbench_candidate(
    session_id: str, candidate_id: str, user: User, _: Enabled
):
    return public_data(
        await db_client.wb_get(
            user.selected_organization_id, session_id, "candidates", candidate_id
        )
    )


@router.post("/sessions/{session_id}/suites", response_model=WorkbenchResponse)
async def workbench_suite(
    session_id: str, body: SimulationSuite, user: User, _: Enabled
):
    return public_data(
        await db_client.wb_save_suite(
            user.selected_organization_id, session_id, body.model_dump()
        )
    )


@router.post("/sessions/{session_id}/evaluations", response_model=WorkbenchResponse)
async def workbench_evaluate(
    session_id: str, body: EvaluateRequest, user: User, _: Enabled
):
    return public_data(
        await enqueue(
            user.selected_organization_id,
            session_id,
            "evaluate",
            body.model_dump(exclude={"idempotency_key"}),
            body.idempotency_key,
        )
    )


@router.post("/sessions/{session_id}/tune", response_model=WorkbenchResponse)
async def workbench_tune(session_id: str, body: TuneRequest, user: User, _: Enabled):
    return public_data(
        await enqueue(
            user.selected_organization_id,
            session_id,
            "tune",
            body.model_dump(exclude={"idempotency_key"}),
            body.idempotency_key,
        )
    )


@router.post(
    "/sessions/{session_id}/jobs/{job_id}/cancel", response_model=WorkbenchResponse
)
async def workbench_cancel(session_id: str, job_id: str, user: User, _: Enabled):
    return public_data(
        await db_client.wb_cancel(user.selected_organization_id, session_id, job_id)
    )


@router.post(
    "/sessions/{session_id}/candidates/{candidate_id}/adopt",
    response_model=WorkbenchResponse,
)
async def workbench_adopt(session_id: str, candidate_id: str, user: User, _: Enabled):
    return public_data(
        await db_client.wb_adopt(
            user.selected_organization_id, session_id, candidate_id
        )
    )


@router.post("/sessions/{session_id}/voice-tool", response_model=WorkbenchResponse)
async def workbench_voice_tool(
    session_id: str, body: VoiceToolRequest, user: User, _: Enabled
):
    return public_data(
        await voice_command(user.selected_organization_id, session_id, body)
    )


@router.websocket("/sessions/{session_id}/voice")
async def workbench_voice(
    websocket: WebSocket,
    session_id: str,
    user: Annotated[UserModel, Depends(get_user_ws)],
):
    from api.services.agent_workbench.voice import serve_voice

    if not WorkbenchSettings.load().enabled or not user.selected_organization_id:
        await websocket.close(code=1008)
        return
    await serve_voice(websocket, user, session_id)

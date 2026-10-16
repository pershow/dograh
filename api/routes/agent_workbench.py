"""Opt-in workbench API. Every child lookup is rooted in the authenticated org."""

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
)
from fastapi.routing import APIRoute

from api.db import db_client
from api.db.models import UserModel
from api.schemas.agent_workbench import (
    CreateSession,
    EvaluateRequest,
    Instruction,
    SessionSettings,
    SimulationSuite,
    TuneRequest,
    VoiceToolRequest,
    WorkbenchResponse,
)
from api.services.agent_workbench.attachments import parse_attachment, upload_attachment
from api.services.agent_workbench.config import WorkbenchSettings
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
    return WorkbenchResponse(data=WorkbenchSettings.load().capabilities())


@router.post("/sessions", response_model=WorkbenchResponse)
async def workbench_create_session(body: CreateSession, user: User, _: Enabled):
    return public_data(
        await db_client.wb_create_session(
            user.selected_organization_id, user.id, **body.model_dump()
        )
    )


@router.get("/sessions/{session_id}", response_model=WorkbenchResponse)
async def workbench_session(session_id: str, user: User, _: Enabled):
    return public_data(
        await db_client.wb_context(user.selected_organization_id, session_id)
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


@router.get("/sessions/{session_id}/events", response_model=WorkbenchResponse)
async def workbench_events(
    session_id: str, user: User, _: Enabled, after: int = Query(0, ge=0)
):
    return WorkbenchResponse(
        data={
            "events": await db_client.wb_events(
                user.selected_organization_id, session_id, after
            )
        }
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


@router.post("/sessions/{session_id}/assets", response_model=WorkbenchResponse)
async def workbench_upload(
    session_id: str, user: User, _: Enabled, file: Annotated[UploadFile, File()]
):
    asset = await upload_attachment(user.selected_organization_id, session_id, file)
    asset.pop("object_key", None)
    return public_data(asset)


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

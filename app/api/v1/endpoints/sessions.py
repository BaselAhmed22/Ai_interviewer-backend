import asyncio
import json
import logging
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import get_current_user_id
from app.core.config import settings
from app.db.models.session import InterviewSession, SessionStatus
from app.workers.tasks import generate_final_interview_report
from app.services import session_service
from app.services.livekit_service import livekit_service
from app.services.redis_service import redis_service
from app.schemas.session import (
    SessionStartResponse,
    SessionListItem,
    SessionResponse,
    InterviewDetailResponse,
    CandidateSummaryResponse,
    EndSessionResponse,
    ReconnectTokenRequest,
    ReconnectTokenResponse,
    AgentStatusResponse,
)
from app.schemas.report import ReportResponse

router = APIRouter()
logger = logging.getLogger(__name__)


async def _dispatch_agent_and_log(room_name: str, session_id: uuid.UUID, metadata: str = "") -> None:
    try:
        await asyncio.wait_for(livekit_service.dispatch_agent(room_name, metadata=metadata), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Agent dispatch timed out for session %s — LiveKit may be unavailable.", session_id)
    except Exception as exc:
        logger.error("Agent dispatch failed for session %s: %s", session_id, exc)


async def _close_room_and_log(room_name: str, session_id: uuid.UUID) -> None:
    # The room is created lazily by the agent's dispatch, which can still
    # be in flight when a session ends moments after starting — this first
    # delete can race it and find nothing. The second pass below, a few
    # seconds later, closes that window.
    try:
        await asyncio.wait_for(livekit_service.close_room(room_name), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Closing LiveKit room timed out for session %s.", session_id)
    except Exception as exc:
        logger.info("Could not close LiveKit room for session %s (may already be closed): %s", session_id, exc)

    await asyncio.sleep(5.0)
    try:
        await asyncio.wait_for(livekit_service.close_room(room_name), timeout=10.0)
    except Exception:
        pass


@router.get("", response_model=list[SessionListItem])
async def list_sessions(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    limit: int = 20,
    offset: int = 0,
):
    user_uuid = uuid.UUID(user_id)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    result = await db.execute(
        select(InterviewSession)
        .where(InterviewSession.user_id == user_uuid)
        .order_by(InterviewSession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    sessions = result.scalars().all()

    return [
        SessionListItem(
            id=str(s.id),
            room_name=s.room_name,
            status=s.status.value,
            created_at=s.created_at,
        )
        for s in sessions
    ]


@router.get("/active", response_model=SessionResponse)
async def get_active_session(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """The caller's current IN_PROGRESS session, if any — at most one can
    ever exist per user (enforced at the DB level). 404 if none."""
    result = await db.execute(
        select(InterviewSession).where(
            InterviewSession.user_id == uuid.UUID(user_id),
            InterviewSession.status == SessionStatus.IN_PROGRESS,
        )
    )
    session_obj = result.scalars().first()
    if not session_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "no_active_session", "message": "No active interview session for this user."},
        )
    return SessionResponse(
        id=str(session_obj.id),
        user_id=str(session_obj.user_id),
        room_name=session_obj.room_name,
        status=session_obj.status.value,
    )


@router.post("/start", response_model=SessionStartResponse, status_code=status.HTTP_201_CREATED)
async def start_session(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        # JWT "sub" is a plain string — must be a real UUID before it
        # touches any UUID-typed column.
        user_uuid = uuid.UUID(user_id)

        # Raises its own HTTPException if the user isn't ready to start
        # (incomplete profile, already active, no CV, no job description)
        # — see app/services/session_service.py. Returns the User row so
        # its name can go straight into the dispatch metadata below.
        user = await session_service.ensure_ready_to_start(db, user_uuid)

        session_id = uuid.uuid4()
        room_name = session_service.build_room_name(session_id)

        # Token generated before the DB row exists: if this failed after
        # the commit, the session would be stuck IN_PROGRESS with no valid
        # token, and the already-active check above would block any retry.
        try:
            livekit_token = livekit_service.generate_token(room_name=room_name, participant_identity=user_id)
        except Exception as exc:
            logger.error("LiveKit token generation failed for user %s: %s", user_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "livekit_unavailable", "message": "Could not start the interview session. Please try again."},
            )

        new_session = await session_service.create_active_session(db, user_uuid, session_id=session_id)

        # Dispatched only after the session row is committed, so a failed
        # dispatch never leaves the agent waiting in a room tied to a
        # session that doesn't exist. Run as a background task since
        # dispatch_agent's liveness check takes a couple of seconds and
        # shouldn't hold up the candidate's token response — see
        # app/workers/livekit_agent.py's entrypoint() for how this
        # metadata is read back.
        dispatch_metadata = json.dumps({
            "session_id": str(session_id),
            "user_id": str(user.id),
            "user_name": f"{user.first_name} {user.last_name}".strip(),
        })
        background_tasks.add_task(_dispatch_agent_and_log, room_name, session_id, dispatch_metadata)

        return SessionStartResponse(
            id=str(new_session.id),
            user_id=str(new_session.user_id),
            room_name=new_session.room_name,
            status=new_session.status.value,
            livekit_token=livekit_token,
            livekit_server_url=settings.LIVEKIT_URL,
            interviewer_title="AI Interview Specialist",
            total_questions=5,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in POST /sessions/start for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "session_start_failed",
                "message": "Could not start the interview session due to an unexpected server error.",
            },
        )


@router.post("/{session_id}/reconnect-token", response_model=ReconnectTokenResponse)
async def get_reconnect_token(
    session_id: uuid.UUID,
    payload: ReconnectTokenRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_id))
    session_obj = result.scalars().first()

    if not session_obj:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
    if str(session_obj.user_id) != user_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Not your session."})
    if session_obj.status != SessionStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=409, detail={"code": "session_not_active", "message": "Session is not active."}
        )

    try:
        token = livekit_service.generate_token(
            room_name=session_obj.room_name,
            participant_identity=user_id,
            participant_name=payload.participant_name,
        )
    except Exception as exc:
        logger.error("LiveKit reconnect token generation failed for session %s: %s", session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "livekit_unavailable", "message": "Could not generate a reconnect token. Please try again."},
        )

    return ReconnectTokenResponse(token=token, server_url=settings.LIVEKIT_URL, room_name=session_obj.room_name)


@router.post("/end/{session_id}", response_model=EndSessionResponse)
async def end_session(
    session_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_id))
    session_obj = result.scalars().first()

    if not session_obj:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
    if str(session_obj.user_id) != user_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Not your session."})
    if session_obj.status != SessionStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "session_not_active",
                "message": "This session has already ended and cannot be ended again.",
            },
        )

    session_obj.status = SessionStatus.COMPLETED
    await db.commit()

    # Tear the room down now — otherwise the agent keeps sitting in it,
    # running STT/LLM/TTS, until it times out on its own.
    background_tasks.add_task(_close_room_and_log, session_obj.room_name, session_id)

    # .delay() is blocking — run off the event loop with a hard timeout so
    # a dead broker can't stall the response. reconcile_missing_reports
    # (app.workers.tasks) retries this later if dispatch fails here.
    task_id: str | None = None
    try:
        task = await asyncio.wait_for(
            asyncio.to_thread(generate_final_interview_report.delay, str(session_id)),
            timeout=3.0,
        )
        task_id = task.id
    except asyncio.TimeoutError:
        logger.warning("Celery dispatch timed out for session %s — broker may be unavailable.", session_id)
    except Exception as exc:
        logger.error("Celery dispatch failed for session %s: %s", session_id, exc)

    return EndSessionResponse(
        message=(
            "Session finished successfully. Report generation started in background."
            if task_id
            else "Session finished. Report generation will be retried automatically shortly."
        ),
        session_id=str(session_id),
        task_id=task_id,
    )


@router.get("/{session_id}/report", response_model=ReportResponse)
async def get_session_report(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    # Eager-load the 1:1 report in the same query instead of a second SELECT.
    session_result = await db.execute(
        select(InterviewSession)
        .options(selectinload(InterviewSession.report))
        .where(InterviewSession.id == session_id)
    )
    session_obj = session_result.scalars().first()

    if not session_obj:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
    if str(session_obj.user_id) != user_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Not your session."})

    if session_obj.status == SessionStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "generation_failed",
                "message": session_obj.failure_reason or "Report generation failed permanently.",
            },
        )

    if not session_obj.report:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "report_not_ready",
                "message": "Report not found. The session might still be processing.",
            },
        )

    return ReportResponse.model_validate(session_obj.report)


@router.get("/{session_id}/agent-status", response_model=AgentStatusResponse)
async def get_agent_status(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Live health of the AI interviewer, as last reported by the LiveKit
    agent process itself (app/workers/livekit_agent.py)."""
    result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_id))
    session_obj = result.scalars().first()

    if not session_obj:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
    if str(session_obj.user_id) != user_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Not your session."})

    data = await redis_service.get_agent_status(session_obj.room_name)
    if not data:
        return AgentStatusResponse(status="unknown", detail=None, updated_at=None)

    return AgentStatusResponse(
        status=data.get("status", "unknown"),
        detail=data.get("detail") or None,
        updated_at=data.get("updated_at"),
    )


@router.get("/{session_id}", response_model=InterviewDetailResponse)
async def get_session_detail(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Full detail view for one past interview — the history page's
    "click into an interview" endpoint: metadata, the prepared question
    list (if any — only the multi-agent pipeline generates one), and the
    evaluation report once it exists."""
    result = await db.execute(
        select(InterviewSession)
        .options(selectinload(InterviewSession.report))
        .where(InterviewSession.id == session_id)
    )
    session_obj = result.scalars().first()

    if not session_obj:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
    if str(session_obj.user_id) != user_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Not your session."})

    report = ReportResponse.model_validate(session_obj.report) if session_obj.report else None
    transcript = None
    if report and report.detailed_metrics:
        transcript = report.detailed_metrics.get("transcript")

    return InterviewDetailResponse(
        id=str(session_obj.id),
        user_id=str(session_obj.user_id),
        room_name=session_obj.room_name,
        status=session_obj.status.value,
        created_at=session_obj.created_at,
        updated_at=session_obj.updated_at,
        questions=session_obj.questions,
        candidate_summary=(
            CandidateSummaryResponse.model_validate(session_obj.candidate_summary)
            if session_obj.candidate_summary
            else None
        ),
        failure_reason=session_obj.failure_reason,
        report=report,
        transcript=transcript,
    )
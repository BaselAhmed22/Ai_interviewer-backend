import asyncio
import logging
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import get_current_user_id
from app.core.config import settings
from app.interviews.models.session import InterviewSession, SessionStatus
from app.interviews.models.report import InterviewReport
from app.workers.tasks import generate_final_interview_report
from app.interviews.services import transcript_service
from app.interviews.services.livekit_service import livekit_service
from app.core.redis_service import redis_service
from app.interviews.schemas.session import (
    SessionListItem,
    SessionResponse,
    InterviewDetailResponse,
    CandidateSummaryResponse,
    TranscriptResponse,
    TranscriptTurnResponse,
    EndSessionResponse,
    ReconnectTokenRequest,
    ReconnectTokenResponse,
    AgentStatusResponse,
)
from app.interviews.schemas.report import EvaluationReportResponse, ReportPendingResponse

router = APIRouter()
logger = logging.getLogger(__name__)


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

    # Single query, no N+1: job_position/companyName are plain columns on
    # InterviewSession itself (snapshotted at /interviews/start — see
    # InterviewSession.job_title's docstring), and overall_score comes
    # from a LEFT OUTER JOIN to interview_reports so a session with no
    # report yet (still in progress, or completed but not yet scored)
    # still returns a row, just with overall_score=None instead of
    # dropping the session from the list entirely.
    result = await db.execute(
        select(InterviewSession, InterviewReport.overall_score)
        .outerjoin(InterviewReport, InterviewReport.session_id == InterviewSession.id)
        .where(InterviewSession.user_id == user_uuid)
        .order_by(InterviewSession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    return [
        SessionListItem(
            id=str(s.id),
            room_name=s.room_name,
            status=s.status.value,
            created_at=s.created_at,
            job_position=s.job_title,
            company_name=s.company_name,
            overall_score=overall_score,
        )
        for s, overall_score in result.all()
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


@router.get(
    "/{session_id}/report",
    response_model=EvaluationReportResponse,
    responses={202: {"model": ReportPendingResponse, "description": "Session ended; report still generating."}},
)
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
        if session_obj.status == SessionStatus.COMPLETED:
            # end_session already enqueued generate_final_interview_report
            # (and reconcile_missing_reports retries it if that dispatch
            # was lost) — this is normal, brief post-interview lag, not an
            # error, so the UI shouldn't treat it as a 404. See
            # ReportPendingResponse's docstring.
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=jsonable_encoder(ReportPendingResponse(session_id=str(session_id))),
            )
        # IN_PROGRESS: the interview hasn't ended yet, so no report was
        # ever enqueued — genuinely "not found" rather than "pending".
        raise HTTPException(
            status_code=404,
            detail={
                "code": "report_not_ready",
                "message": "Report not found. The session might still be processing.",
            },
        )

    return EvaluationReportResponse.from_report(session_obj.report)


@router.get("/{session_id}/agent-status", response_model=AgentStatusResponse)
async def get_agent_status(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Live health of the AI interviewer, as last reported by the LiveKit
    agent process itself (app/interviews/workers/livekit_agent.py)."""
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


@router.get("/{session_id}/transcript", response_model=TranscriptResponse)
async def get_session_transcript(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """The full candidate/agent conversation log, captured live by the
    LiveKit agent and written once the interview ends (see
    app.interviews.services.transcript_service and InterviewerAgent.on_exit() in
    app/interviews/agents/voice_agent.py). This is the input the real evaluator will
    eventually score against."""
    result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_id))
    session_obj = result.scalars().first()

    if not session_obj:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
    if str(session_obj.user_id) != user_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Not your session."})

    transcript = await transcript_service.get_transcript(db, session_id)
    if not transcript or not transcript.turns:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "transcript_not_ready",
                "message": "Transcript not available yet. It's saved once the interview ends.",
            },
        )

    return TranscriptResponse(
        session_id=str(session_id),
        turns=[TranscriptTurnResponse.model_validate(turn) for turn in transcript.turns],
        updated_at=transcript.updated_at,
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
        .options(selectinload(InterviewSession.report), selectinload(InterviewSession.transcript))
        .where(InterviewSession.id == session_id)
    )
    session_obj = result.scalars().first()

    if not session_obj:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
    if str(session_obj.user_id) != user_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Not your session."})

    report = EvaluationReportResponse.from_report(session_obj.report) if session_obj.report else None
    transcript = (
        [TranscriptTurnResponse.model_validate(turn) for turn in session_obj.transcript.turns]
        if session_obj.transcript and session_obj.transcript.turns
        else None
    )

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
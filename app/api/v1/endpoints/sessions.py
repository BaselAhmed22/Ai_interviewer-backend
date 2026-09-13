# app/api/v1/endpoints/sessions.py
import asyncio
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
    EndSessionResponse,
    ReconnectTokenRequest,
    ReconnectTokenResponse,
    AgentStatusResponse,
)
from app.schemas.report import ReportResponse

router = APIRouter()
logger = logging.getLogger(__name__)


async def _dispatch_agent_and_log(room_name: str, session_id: uuid.UUID) -> None:
    try:
        await asyncio.wait_for(livekit_service.dispatch_agent(room_name), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Agent dispatch timed out for session %s — LiveKit may be unavailable.", session_id)
    except Exception as exc:
        logger.error("Agent dispatch failed for session %s: %s", session_id, exc)


async def _close_room_and_log(room_name: str, session_id: uuid.UUID) -> None:
    # A room doesn't exist on the LiveKit server until someone actually
    # connects to it — start_session only mints a token; the room itself
    # gets created as a side effect of the agent's dispatch (dispatch_agent
    # has its own ~1.5s+ built-in delay for its liveness check). A session
    # ended within moments of being started can race that: this first
    # delete finds nothing (silently succeeds — LiveKit's delete is
    # idempotent, not an error), and the still-in-flight dispatch then
    # creates the room *after*, with nothing left to clean it up. A second
    # pass a few seconds later closes that window without adding real
    # delay to the common case, which the first attempt already handles.
    try:
        await asyncio.wait_for(livekit_service.close_room(room_name), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Closing LiveKit room timed out for session %s.", session_id)
    except Exception as exc:
        # Deleting a room that's already gone (everyone already left
        # naturally) is an expected, harmless case here — best-effort
        # cleanup, not a required step.
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


@router.post("/start", response_model=SessionStartResponse, status_code=status.HTTP_201_CREATED)
async def start_session(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        # JWT "sub" arrives as a plain string — convert to a real UUID object
        # once, up front, before it touches any UUID-typed column. Comparing
        # or inserting a raw str against InterviewSession.user_id /
        # CandidateProfile.user_id / JobDescription.user_id (all UUID columns)
        # is what was causing the 500s on this endpoint.
        user_uuid = uuid.UUID(user_id)

        # Raises the appropriate HTTPException itself if this user isn't
        # ready to start (incomplete profile, already active, no CV, CV
        # failed, no job description) — see app/services/session_service.py.
        await session_service.ensure_ready_to_start(db, user_uuid)

        session_id = uuid.uuid4()
        room_name = session_service.build_room_name(session_id)

        # Generate the LiveKit token *before* creating the DB row. If this
        # were done after the commit and it failed, the session would be
        # stuck in IN_PROGRESS forever with no valid token ever returned —
        # and the "session already active" check above would then
        # permanently block this user from ever starting a new interview.
        try:
            livekit_token = livekit_service.generate_token(room_name=room_name, participant_identity=user_id)
        except Exception as exc:
            logger.error("LiveKit token generation failed for user %s: %s", user_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "livekit_unavailable", "message": "Could not start the interview session. Please try again."},
            )

        new_session = await session_service.create_active_session(db, user_uuid, session_id=session_id)

        # Dispatched only now that the session row is durably committed —
        # dispatching before commit could hand the agent a room for a
        # session that then fails to persist (e.g. loses the same-user race
        # above), leaving it waiting alone in a room nobody will ever join.
        #
        # Run as a background task rather than awaited here: dispatch_agent
        # includes a short liveness check (does a worker actually pick the
        # job up) that takes a couple of seconds by design — worth the wait
        # for an accurate log, not worth making the candidate's token
        # response wait for it. Same "best-effort" spirit as the
        # report-generation dispatch in end_session: a dispatch problem
        # (LiveKit unreachable, no worker connected) gets logged loudly,
        # but doesn't block the candidate from getting their token and
        # joining the room — automatic server-side dispatch, if configured,
        # can still pick it up independently.
        background_tasks.add_task(_dispatch_agent_and_log, room_name, session_id)

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
        # Anything not already turned into a clean HTTPException above
        # (a bad LiveKit key format, a DB/schema mismatch, ...) — log the
        # full traceback here, with the user_id this failed for, instead of
        # letting the client see a bare 500 with no way to trace it back to
        # a specific request in the server log.
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

    # Tear the LiveKit room down now that the interview is officially
    # over — otherwise the agent (still running STT/LLM/TTS) can keep
    # sitting in a room nobody will ever rejoin, on a session that's
    # already COMPLETED here, until it times out on its own.
    background_tasks.add_task(_close_room_and_log, session_obj.room_name, session_id)

    # Dispatch Celery task safely — wrap the blocking .delay() call in
    # asyncio.to_thread so it never blocks the event loop, and impose a
    # hard 3-second timeout so a dead/slow broker cannot stall the HTTP
    # response. If dispatch fails for any reason we still return 200 to
    # the client; the periodic reconciliation task in app.workers.tasks
    # (reconcile_missing_reports) will pick this session up and retry
    # the dispatch on its own within a few minutes.
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
    # One round trip instead of two separate SELECTs: the session<->report
    # relationship is already 1:1 (InterviewReport.session_id is unique),
    # so eager-loading it here means session_obj.report is populated for
    # free instead of a second query keyed off session_id.
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
    """
    Live health of the AI interviewer for this session, as last reported
    by the LiveKit agent process itself (app/workers/livekit_agent.py) —
    not something this endpoint infers. Lets the app show "Aria is having
    trouble responding" instead of a candidate sitting in silence with no
    indication whether that's expected or a failure.
    """
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
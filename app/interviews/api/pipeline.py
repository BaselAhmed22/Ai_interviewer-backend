"""
Multi-agent interview pipeline: DocumentAgent -> QuestionnaireAgent
(prepare) -> ControllerAgent -> VoiceAgent (start) -> EvaluationAgent
(evaluate). See app/interviews/agents/ for the five agent modules and
app/interviews/services/interview_pipeline.py for the orchestrator.

These sit alongside, not instead of, the existing /sessions/* and
/interview/start endpoints — those remain the simple, single-call path
with no prepared question list. This pipeline is for clients that want
the candidate summary and generated questions back (and a place to hand
them to the voice agent) before the interview starts.
"""
import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_active_user
from app.core.security import get_current_user_id
from app.interviews.models.session import InterviewSession, SessionStatus
from app.auth.models import User, is_admin
from app.interviews.services import session_service
from app.interviews.services.interview_pipeline import interview_pipeline_manager
from app.interviews.services.livekit_service import livekit_service
from app.workers.tasks import generate_final_interview_report
from app.interviews.schemas.interview_pipeline import (
    PrepareInterviewRequest,
    PrepareInterviewResponse,
    StartPipelineRequest,
    StartPipelineResponse,
    EvaluateInterviewRequest,
    EvaluateInterviewResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def _dispatch_agent_and_log(room_name: str, session_id: uuid.UUID, metadata: str) -> None:
    try:
        await asyncio.wait_for(livekit_service.dispatch_agent(room_name, metadata=metadata), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Agent dispatch timed out for session %s — LiveKit may be unavailable.", session_id)
    except Exception as exc:
        logger.error("Agent dispatch failed for session %s: %s", session_id, exc)


_TRIAL_LIMIT_MESSAGE = (
    "You have finished your free attempts. If you want more attempts, "
    "please contact b.ahmed22@ieee.org"
)


@router.post("/prepare", response_model=PrepareInterviewResponse)
async def prepare_interview(
    payload: PrepareInterviewRequest = PrepareInterviewRequest(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Stage 1-2: run DocumentAgent then QuestionnaireAgent against the
    caller's active CV and job description, and persist the result.
    get_current_active_user already blocks accounts pending admin
    approval; admins are additionally exempt from the free-attempt cap."""
    if not is_admin(user) and user.interview_attempts_used >= settings.FREE_INTERVIEW_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "trial_limit_reached", "message": _TRIAL_LIMIT_MESSAGE},
        )

    try:
        context = await interview_pipeline_manager.prepare(db, user.id, simli_face_id=payload.simli_face_id)
    except HTTPException:
        raise
    except RuntimeError as exc:
        # GEMINI_API_KEY not configured — a deployment/config gap, not
        # something the candidate can do anything about.
        logger.error("Interview preparation misconfigured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ai_not_configured", "message": "Interview preparation is not available right now."},
        )
    except Exception:
        # Gemini unreachable, quota exhausted, or returned something
        # unusable — logged in full here so the actual cause is visible,
        # the candidate just gets a clean, retryable response.
        logger.exception("Interview preparation failed for user %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ai_generation_failed", "message": "Could not prepare the interview. Please try again."},
        )

    if not is_admin(user):
        user.interview_attempts_used += 1
        await db.commit()

    return PrepareInterviewResponse(
        preparation_id=context.preparation_id,
        questions_count=len(context.questions),
        candidate_headline=context.candidate_summary.headline if context.candidate_summary else None,
        remaining_attempts=max(0, settings.FREE_INTERVIEW_ATTEMPTS - user.interview_attempts_used),
    )


@router.post("/start", response_model=StartPipelineResponse, status_code=status.HTTP_201_CREATED)
async def start_interview_pipeline(
    payload: StartPipelineRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Stage 3-4: ControllerAgent takes ownership of the prepared question
    list, VoiceAgent is dispatched into a fresh LiveKit room — carrying
    preparation_id as dispatch metadata so the agent process can load this
    same context back out of Redis and brief itself with it."""
    context = await interview_pipeline_manager.get_context(payload.preparation_id)
    if context.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Not your interview preparation."},
        )

    user_uuid = uuid.UUID(user_id)
    await session_service.ensure_ready_to_start(db, user_uuid)

    session_id = uuid.uuid4()
    room_name = session_service.build_room_name(session_id)

    # Token generated before the DB row exists — same reasoning as
    # /sessions/start: a token-generation failure after the row was
    # already committed would leave the session stuck IN_PROGRESS with no
    # valid token ever returned.
    try:
        livekit_token = livekit_service.generate_token(room_name=room_name, participant_identity=user_id)
    except Exception as exc:
        logger.error("LiveKit token generation failed for user %s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "livekit_unavailable", "message": "Could not start the interview session. Please try again."},
        )

    new_session = await session_service.create_active_session(
        db,
        user_uuid,
        session_id=session_id,
        questions=[q.model_dump() for q in context.questions],
        candidate_summary=context.candidate_summary.model_dump() if context.candidate_summary else None,
        simli_face_id=context.simli_face_id,
    )
    context = await interview_pipeline_manager.mark_started(context, new_session.id)

    # session_id/user_id/simli_face_id travel alongside preparation_id (not
    # just the bare preparation_id string as before) so the agent can fall
    # back to the durable copy on the session row (questions +
    # candidate_summary, persisted above) if the Redis pipeline context has
    # expired, and always has the chosen avatar face without a Redis lookup.
    dispatch_metadata = json.dumps({
        "preparation_id": context.preparation_id,
        "session_id": str(new_session.id),
        "user_id": user_id,
        "simli_face_id": context.simli_face_id,
    })
    background_tasks.add_task(_dispatch_agent_and_log, room_name, new_session.id, dispatch_metadata)

    return StartPipelineResponse(
        session_id=str(new_session.id),
        room_name=room_name,
        livekit_token=livekit_token,
        livekit_server_url=settings.LIVEKIT_URL,
    )


@router.post("/evaluate", response_model=EvaluateInterviewResponse)
async def evaluate_interview(
    payload: EvaluateInterviewRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Stage 5: EvaluationAgent, dispatched through the same durable,
    retried Celery path as the automatic evaluation POST
    /sessions/end/{id} already triggers — safe to call again for a
    session that already has a report (generate_final_interview_report's
    own duplicate-dispatch handling is a no-op in that case)."""
    try:
        session_uuid = uuid.UUID(payload.session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})

    result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_uuid))
    session_obj = result.scalars().first()
    if not session_obj:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
    if str(session_obj.user_id) != user_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Not your session."})
    if session_obj.status != SessionStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "session_not_completed",
                "message": "The interview must be ended before it can be evaluated.",
            },
        )

    task_id: str | None = None
    try:
        task = await asyncio.wait_for(
            asyncio.to_thread(generate_final_interview_report.delay, str(session_uuid)),
            timeout=3.0,
        )
        task_id = task.id
    except asyncio.TimeoutError:
        logger.warning("Celery dispatch timed out for session %s.", session_uuid)
    except Exception as exc:
        logger.error("Celery dispatch failed for session %s: %s", session_uuid, exc)

    return EvaluateInterviewResponse(
        message="Evaluation started." if task_id else "Evaluation will be retried automatically shortly.",
        session_id=str(session_uuid),
        task_id=task_id,
    )

"""
Multi-agent interview pipeline: DocumentAgent -> QuestionnaireAgent
(prepare) -> ControllerAgent -> VoiceAgent (start) -> EvaluationAgent
(evaluate). See app/interviews/agents/ for the five agent modules and
app/interviews/services/interview_pipeline.py for the orchestrator.

This is the only supported way to create an interview session — the
direct, no-prepared-questions POST /sessions/start path was removed once
every client moved to prepare -> start. /sessions/* (app/interviews/api/sessions.py)
still owns the lifecycle/read endpoints (end, detail, transcript,
report) for sessions this pipeline creates.

Both /prepare (JSON, pre-extracted cv_text) and /prepare/upload
(multipart, a raw PDF/DOCX) are fully self-contained: no separate
candidate-profile/job-description setup call is needed first, and there
is no longer a standalone /candidates/* API surface at all — this module
creates the CandidateProfile/JobDescription rows it needs internally
(app.candidates.models/services remain as internal building blocks).
"""
import asyncio
import json
import logging
import re
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from google.genai import errors as genai_errors
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_active_user
from app.core.security import get_current_user_id
from app.interviews.models.session import InterviewSession, SessionStatus
from app.interviews.schemas.agent_context import AgentContext
from app.candidates.models import CandidateProfile, JobDescription
from app.candidates.services.candidate_service import save_as_active_record, save_uploaded_cv
from app.candidates.services.cv_parser import extract_text_from_file, CorruptFileError
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

_QUOTA_EXCEEDED_MESSAGE = "AI service quota exhausted, please try again later or upgrade plan."


def _is_quota_exceeded(exc: genai_errors.ClientError) -> bool:
    return exc.code == 429 or (exc.status or "").upper() == "RESOURCE_EXHAUSTED"


def _retry_after_seconds(exc: genai_errors.ClientError) -> int | None:
    """Best-effort: Gemini sometimes includes a google.rpc.RetryInfo entry
    in the error's details with a retryDelay like "20s" — surface it as a
    standard Retry-After header when present so a well-behaved client
    backs off instead of hammering the same failing request. Absence of
    this (free-tier quota errors often omit it) is normal, not a bug."""
    details = exc.details if isinstance(exc.details, dict) else {}
    error_body = details.get("error", details)
    error_details = error_body.get("details", []) if isinstance(error_body, dict) else []
    if not isinstance(error_details, list):
        return None
    for entry in error_details:
        if not isinstance(entry, dict):
            continue
        if entry.get("@type", "").endswith("RetryInfo"):
            match = re.match(r"^(\d+)", str(entry.get("retryDelay", "")))
            if match:
                return int(match.group(1))
    return None


def _check_trial_limit(user: User) -> None:
    if not is_admin(user) and user.interview_attempts_used >= settings.FREE_INTERVIEW_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "trial_limit_reached", "message": _TRIAL_LIMIT_MESSAGE},
        )


async def _run_prepare_pipeline(
    db: AsyncSession,
    user: User,
    company_name: Optional[str],
    force_regenerate: bool,
    number_of_questions: int,
) -> AgentContext:
    """Shared core of POST /prepare and POST /prepare/upload: runs
    InterviewPipelineManager.prepare() (DocumentAgent -> QuestionnaireAgent,
    each retrying once or twice on a transient Gemini 5xx — see
    app.interviews.agents.gemini_retry) and translates every failure mode
    into the same clean HTTPException both routes return."""
    try:
        return await interview_pipeline_manager.prepare(
            db,
            user.id,
            company_name=company_name,
            force_regenerate=force_regenerate,
            number_of_questions=number_of_questions,
        )
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
    except genai_errors.ServerError as exc:
        # Gemini's own 5xx (e.g. 503 UNAVAILABLE) — already retried a
        # couple of times inside DocumentAgent/QuestionnaireAgent
        # (gemini_retry.call_with_retry); still failing means Gemini
        # itself is down, not a request problem, so a distinct code from
        # ai_generation_failed so the client knows retrying later (not
        # differently) is the right move.
        logger.error("Gemini backend error preparing an interview for user %s (after retries): %s", user.id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "ai_temporarily_unavailable",
                "message": "The AI service is temporarily unavailable. Please try again in a moment.",
            },
        )
    except genai_errors.ClientError as exc:
        if _is_quota_exceeded(exc):
            logger.warning("Gemini quota exhausted preparing an interview for user %s: %s", user.id, exc)
            retry_after = _retry_after_seconds(exc)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "ai_quota_exceeded", "message": _QUOTA_EXCEEDED_MESSAGE},
                headers={"Retry-After": str(retry_after)} if retry_after else None,
            )
        # Some other 4xx from Gemini (bad request, invalid key, ...) —
        # not the candidate's fault either way, same clean fallback as
        # the generic branch below.
        logger.exception("Gemini rejected the request preparing an interview for user %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ai_generation_failed", "message": "Could not prepare the interview. Please try again."},
        )
    except Exception:
        # Gemini unreachable, or returned something unusable — logged in
        # full here so the actual cause is visible, the candidate just
        # gets a clean, retryable response.
        logger.exception("Interview preparation failed for user %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ai_generation_failed", "message": "Could not prepare the interview. Please try again."},
        )


@router.post("/prepare", response_model=PrepareInterviewResponse)
async def prepare_interview(
    payload: PrepareInterviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Fully self-contained: no prior candidate-profile/job-description
    setup needed — cv_text/job_title/job_description arrive directly in
    this request. Creates the CandidateProfile/JobDescription rows
    InterviewPipelineManager.prepare() needs (as this user's new active
    ones) from that payload, then runs DocumentAgent -> QuestionnaireAgent.
    get_current_active_user already blocks accounts pending admin
    approval; admins are additionally exempt from the free-attempt cap.

    POST /interviews/prepare/upload is the equivalent for a raw PDF/DOCX
    file instead of already-extracted text."""
    _check_trial_limit(user)

    await save_as_active_record(
        db,
        CandidateProfile,
        user.id,
        lambda: CandidateProfile(user_id=user.id, raw_cv_text=payload.cv_text, is_active=True),
    )
    await save_as_active_record(
        db,
        JobDescription,
        user.id,
        lambda: JobDescription(
            user_id=user.id, job_title=payload.job_title, description_text=payload.job_description, is_active=True
        ),
    )

    context = await _run_prepare_pipeline(
        db, user, payload.company_name, payload.force_regenerate, payload.number_of_questions
    )

    if not is_admin(user):
        user.interview_attempts_used += 1
        await db.commit()

    return PrepareInterviewResponse(
        preparation_id=context.preparation_id,
        questions_count=len(context.questions),
        candidate_headline=context.candidate_summary.headline if context.candidate_summary else None,
        remaining_attempts=max(0, settings.FREE_INTERVIEW_ATTEMPTS - user.interview_attempts_used),
        from_cache=context.from_cache,
    )


@router.post("/prepare/upload", response_model=PrepareInterviewResponse)
async def prepare_interview_from_upload(
    cv_file: UploadFile = File(..., description="Candidate's CV — PDF or DOCX."),
    job_title: str = Form(..., max_length=255),
    job_description: str = Form(..., min_length=1, max_length=20000),
    number_of_questions: int = Form(5, ge=3, le=10),
    company_name: Optional[str] = Form(default=None, max_length=255),
    force_regenerate: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """All-in-one variant of POST /prepare for a raw CV file instead of
    already-extracted text: accepts the CV file and job description
    directly in a single multipart request, self-contained like /prepare.
    Runs the exact same trial-limit check, DocumentAgent ->
    QuestionnaireAgent pipeline (with the same interview-prep-cache and
    Gemini error handling, including retry on a transient 5xx) as
    POST /prepare; the only difference is where candidate_profile/
    job_description come from.

    Two notes on what the request does NOT accept, on purpose:
    - No `user_id` field: this endpoint is authenticated exactly like
      every other one in this app (get_current_active_user, from the JWT).
      Trusting a client-supplied user_id instead would let one user run a
      prepare as someone else — the caller is always the authenticated user.
    - `job_title` is required (not just `job_description`): JobDescription.
      job_title is a NOT NULL column used elsewhere (question prompts,
      GET /sessions' dashboard cards) — there's no safe placeholder to
      silently default it to.
    """
    _check_trial_limit(user)

    # 1. CV: save + extract text SYNCHRONOUSLY (there's no more async
    # process_cv_analysis Celery task to defer to — it was removed along
    # with the standalone /candidates/upload-cv endpoint) — DocumentAgent
    # needs raw_cv_text right now, not after a background job completes.
    profile = await save_uploaded_cv(db, user.id, cv_file)
    try:
        profile.raw_cv_text = extract_text_from_file(profile.cv_file_path)
    except CorruptFileError as exc:
        profile.processing_failed = True
        profile.failure_reason = str(exc)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "cv_processing_failed",
                "message": "The uploaded CV could not be read. Please check the file and try again.",
            },
        )
    await db.commit()

    # 2. Job description: same "one active per user" upsert POST /prepare
    # (the JSON variant) also does.
    await save_as_active_record(
        db,
        JobDescription,
        user.id,
        lambda: JobDescription(
            user_id=user.id, job_title=job_title, description_text=job_description, is_active=True
        ),
    )

    # 3. Same pipeline, same error handling, as POST /prepare.
    context = await _run_prepare_pipeline(db, user, company_name, force_regenerate, number_of_questions)

    if not is_admin(user):
        user.interview_attempts_used += 1
        await db.commit()

    return PrepareInterviewResponse(
        preparation_id=context.preparation_id,
        questions_count=len(context.questions),
        candidate_headline=context.candidate_summary.headline if context.candidate_summary else None,
        remaining_attempts=max(0, settings.FREE_INTERVIEW_ATTEMPTS - user.interview_attempts_used),
        from_cache=context.from_cache,
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

    # Token generated before the DB row exists: a token-generation failure
    # after the row was already committed would leave the session stuck
    # IN_PROGRESS with no valid token ever returned.
    try:
        livekit_token = livekit_service.generate_token(room_name=room_name, participant_identity=user_id)
    except Exception as exc:
        logger.error("LiveKit token generation failed for user %s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "livekit_unavailable", "message": "Could not start the interview session. Please try again."},
        )

    # Snapshotted onto the session row (not looked up fresh at read time)
    # so GET /sessions' dashboard cards show what THIS interview was
    # actually for even after the candidate's active job description
    # changes later — see InterviewSession.job_title's docstring.
    # company_name travels on the AgentContext itself (captured directly
    # on the prepare request — see PrepareInterviewRequest.company_name),
    # no separate lookup needed.
    job_result = await db.execute(
        select(JobDescription).where(JobDescription.id == uuid.UUID(context.job_description_id))
    )
    job = job_result.scalars().first()

    new_session = await session_service.create_active_session(
        db,
        user_uuid,
        session_id=session_id,
        questions=[q.model_dump() for q in context.questions],
        candidate_summary=context.candidate_summary.model_dump() if context.candidate_summary else None,
        job_title=job.job_title if job else None,
        company_name=context.company_name,
    )
    context = await interview_pipeline_manager.mark_started(context, new_session.id)

    # session_id/user_id travel alongside preparation_id (not just the
    # bare preparation_id string as before) so the agent can fall back to
    # the durable copy on the session row (questions + candidate_summary,
    # persisted above) if the Redis pipeline context has expired.
    dispatch_metadata = json.dumps({
        "preparation_id": context.preparation_id,
        "session_id": str(new_session.id),
        "user_id": user_id,
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
    user: User = Depends(get_current_active_user),
):
    """Stage 5: Admin-only manual evaluation dispatch.

    Manual evaluation triggers from the frontend are disabled for standard candidate
    accounts. Evaluations are automatically triggered server-side when a session ends."""
    if not is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "forbidden",
                "message": "Manual evaluation triggers are disabled for candidate accounts. Evaluations are triggered automatically when an interview ends.",
            },
        )

    try:
        session_uuid = uuid.UUID(payload.session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})

    result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_uuid))
    session_obj = result.scalars().first()
    if not session_obj:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Session not found."})
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

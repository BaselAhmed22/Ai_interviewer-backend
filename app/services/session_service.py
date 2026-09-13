# Readiness checks for starting an interview session — pulled out of the
# /sessions/start endpoint so that handler reads as "check readiness, then
# create the room/token/row" instead of interleaving all of it inline.
import uuid

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.db.models.session import InterviewSession, SessionStatus
from app.db.models.user import CandidateProfile, JobDescription, User, is_profile_complete


async def ensure_ready_to_start(db: AsyncSession, user_uuid: uuid.UUID) -> None:
    """Raise the appropriate HTTPException if this user can't start a new
    interview session right now (incomplete profile, already has one
    active, no CV, CV failed to process, or no job description on file).
    No-op otherwise."""
    user_result = await db.execute(select(User).where(User.id == user_uuid))
    user = user_result.scalars().first()
    if not user or not is_profile_complete(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "profile_incomplete",
                "message": "Please complete your profile before starting an interview.",
            },
        )

    existing = await db.execute(
        select(InterviewSession).where(
            InterviewSession.user_id == user_uuid,
            InterviewSession.status == SessionStatus.IN_PROGRESS,
        )
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "session_already_active",
                "message": "You already have an interview session in progress.",
            },
        )

    cv_result = await db.execute(
        select(CandidateProfile).where(
            CandidateProfile.user_id == user_uuid, CandidateProfile.is_active.is_(True)
        )
    )
    active_cv = cv_result.scalars().first()
    if not active_cv:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_cv", "message": "Upload a CV before starting a session."},
        )
    if active_cv.processing_failed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "cv_processing_failed",
                "message": "Your CV could not be processed. Please re-upload it before starting a session.",
            },
        )

    job_result = await db.execute(
        select(JobDescription).where(
            JobDescription.user_id == user_uuid, JobDescription.is_active.is_(True)
        )
    )
    if not job_result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_job_description",
                "message": "Add a job description before starting a session.",
            },
        )


def build_room_name(session_id: uuid.UUID) -> str:
    # The full UUID, not a truncated prefix: 8 hex chars is only 32 bits
    # of entropy, and this name is the sole isolation boundary between one
    # candidate's interview room and every other concurrent one — a
    # collision there means two different interviews sharing one LiveKit
    # room (crossed audio/video, wrong participants). The full UUID's
    # collision probability is astronomically lower.
    return f"room_{session_id}"


async def create_active_session(
    db: AsyncSession, user_uuid: uuid.UUID, session_id: uuid.UUID | None = None
) -> InterviewSession:
    """Create and commit a new IN_PROGRESS InterviewSession row. Shared by
    /sessions/start and the multi-agent pipeline's /interviews/start so
    both create sessions identically.

    Pass `session_id` when the caller already generated the LiveKit token
    against a specific room name *before* calling this (the deliberate
    ordering both callers use: if token generation happened after this
    commit and then failed, the session would be stuck IN_PROGRESS with
    no valid token ever returned, and ensure_ready_to_start()'s
    already-active check would then block any retry forever). Omit it to
    let this function generate one.

    Raises 409 if a concurrent request for the same user wins the
    one-active-session-per-user race — the DB-level partial unique index
    is the real guard; call ensure_ready_to_start() first for the normal
    pre-check, this is the race's fallback path."""
    session_id = session_id or uuid.uuid4()
    new_session = InterviewSession(
        id=session_id,
        user_id=user_uuid,
        room_name=build_room_name(session_id),
        status=SessionStatus.IN_PROGRESS,
    )
    db.add(new_session)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "session_already_active",
                "message": "You already have an interview session in progress.",
            },
        )
    await db.refresh(new_session)
    return new_session

import uuid

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.interviews.models.session import InterviewSession, SessionStatus
from app.auth.models import User, is_profile_complete
from app.candidates.models import CandidateProfile, JobDescription
from app.admin.services import system_settings_service


async def ensure_ready_to_start(db: AsyncSession, user_uuid: uuid.UUID) -> User:
    """Raises the appropriate HTTPException if this user can't start a new
    interview session (incomplete profile, already active, no CV, CV
    failed to process, or no job description). Returns the User row
    otherwise, saving the caller a second identical query."""
    user_result = await db.execute(select(User).where(User.id == user_uuid))
    user = user_result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "profile_incomplete",
                "message": "Please complete your profile before starting an interview.",
            },
        )
    settings_row = await system_settings_service.get_settings(db)
    if settings_row.require_admin_approval and not user.is_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "account_pending_approval",
                "message": "Your account is pending admin approval. Please check back shortly.",
            },
        )
    if not is_profile_complete(user):
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

    return user


def build_room_name(session_id: uuid.UUID) -> str:
    # The full UUID, not a truncated prefix — this is the sole isolation
    # boundary between concurrent interview rooms.
    return f"room_{session_id}"


async def create_active_session(
    db: AsyncSession,
    user_uuid: uuid.UUID,
    session_id: uuid.UUID | None = None,
    questions: list[dict] | None = None,
    candidate_summary: dict | None = None,
    simli_face_id: str | None = None,
) -> InterviewSession:
    """Create and commit a new IN_PROGRESS InterviewSession row. Shared by
    /sessions/start and /interviews/start so both create sessions
    identically.

    Pass `session_id` when the caller already generated the LiveKit token
    against that room name before calling this — both callers do this so a
    failed token generation never leaves a session stuck IN_PROGRESS with
    no valid token. Omit it to let this function generate one.

    Raises 409 if a concurrent request wins the one-active-session race —
    the DB-level partial unique index is the real guard; this is its
    fallback path, after ensure_ready_to_start()'s normal pre-check."""
    session_id = session_id or uuid.uuid4()
    new_session = InterviewSession(
        id=session_id,
        user_id=user_uuid,
        room_name=build_room_name(session_id),
        status=SessionStatus.IN_PROGRESS,
        questions=questions,
        candidate_summary=candidate_summary,
        simli_face_id=simli_face_id,
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

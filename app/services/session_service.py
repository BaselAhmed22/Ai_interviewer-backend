# Readiness checks for starting an interview session — pulled out of the
# /sessions/start endpoint so that handler reads as "check readiness, then
# create the room/token/row" instead of interleaving all of it inline.
import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.db.models.session import InterviewSession, SessionStatus
from app.db.models.user import CandidateProfile, JobDescription


async def ensure_ready_to_start(db: AsyncSession, user_uuid: uuid.UUID) -> None:
    """Raise the appropriate HTTPException if this user can't start a new
    interview session right now (already has one active, no CV, CV failed
    to process, or no job description on file). No-op otherwise."""
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

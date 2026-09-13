import asyncio
import logging
import os
import uuid
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
import aiofiles

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user_id
from app.db.models.user import CandidateProfile, JobDescription, InterviewPreference
from app.services.candidate_service import save_as_active_record
from app.services.cv_parser import resolve_cv_path
from app.workers.tasks import process_cv_analysis
from app.schemas.candidate import (
    JobDescriptionRequest,
    InterviewPreferenceRequest,
    CvUploadResponse,
    JobDescriptionResponse,
    InterviewPreferenceResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)

ALLOWED_CV_EXTENSIONS = (".pdf", ".docx")
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/upload-cv", response_model=CvUploadResponse)
async def upload_cv(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    # JWT "sub" is a string — convert to a real UUID object once, up front,
    # before it touches any UUID-typed column.
    user_uuid = uuid.UUID(user_id)

    if not file.filename or not file.filename.lower().endswith(ALLOWED_CV_EXTENSIONS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "unsupported_file_type", "message": "Only PDF and DOCX files are allowed."},
        )

    # Read in bounded chunks and abort as soon as the running total crosses
    # the limit, instead of buffering the whole upload into memory first
    # and only then checking its size — a multi-hundred-MB body named
    # "cv.pdf" would otherwise sit fully in RAM before ever being rejected,
    # and concurrent oversized uploads compound that.
    chunk_size = 1024 * 1024
    max_size = settings.MAX_CV_UPLOAD_SIZE_BYTES
    buffer = bytearray()
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_size:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "file_too_large",
                    "message": f"File exceeds the {max_size // (1024 * 1024)}MB limit.",
                },
            )
    content = bytes(buffer)
    if len(content) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "empty_file", "message": "The uploaded file is empty."},
        )

    file_extension = os.path.splitext(file.filename)[1].lower()
    unique_filename = f"{uuid.uuid4()}{file_extension}"
    # Absolute path, stored as such in the DB below — FastAPI and the
    # Celery worker are separate processes with independent working
    # directories, so a relative path here would resolve differently (or
    # not at all) depending on which directory each happened to be
    # launched from.
    file_path = settings.UPLOAD_DIR / unique_filename

    try:
        async with aiofiles.open(file_path, "wb") as buffer:
            await buffer.write(content)
    except OSError as exc:
        logger.error("Failed to write uploaded CV to disk for user %s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "storage_error", "message": "Could not store the file. Please try again."},
        )

    try:
        profile = await save_as_active_record(
            db,
            CandidateProfile,
            user_uuid,
            lambda: CandidateProfile(
                user_id=user_uuid,
                cv_file_path=str(file_path),
                original_filename=file.filename,
                is_active=True,
            ),
        )
    except Exception:
        # The file already landed on disk before any DB row was created
        # to reference it. If the transaction fails, remove it instead of
        # leaving an orphaned upload with nothing pointing to it.
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise

    # Same event-loop hazard as report generation in sessions.py: .delay()
    # makes a real, synchronous network call to the broker. Isolate it in a
    # thread with a hard timeout so a slow/dead broker can't stall this
    # response — the CV is already saved either way, so dispatch failure is
    # non-fatal and just gets logged.
    try:
        await asyncio.wait_for(
            asyncio.to_thread(process_cv_analysis.delay, str(profile.id)),
            timeout=3.0,
        )
    except asyncio.TimeoutError:
        logger.warning("CV analysis dispatch timed out for profile %s — broker may be unavailable.", profile.id)
    except Exception as exc:
        logger.error("CV analysis dispatch failed for profile %s: %s", profile.id, exc)

    return CvUploadResponse(
        message="CV uploaded successfully and analysis queued",
        profile_id=str(profile.id),
        file_path=f"/api/v1/candidates/cv/{profile.id}/download",
    )


@router.get("/cv/{profile_id}/download")
async def download_cv(
    profile_id: uuid.UUID,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(CandidateProfile).where(CandidateProfile.id == profile_id))
    profile = result.scalars().first()

    if not profile:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "CV not found."})
    if str(profile.user_id) != user_id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Not your CV."})

    resolved_path = resolve_cv_path(profile.cv_file_path) if profile.cv_file_path else None
    if not resolved_path or not resolved_path.exists():
        raise HTTPException(status_code=404, detail={"code": "file_missing", "message": "CV file not found on server."})

    return FileResponse(
        path=str(resolved_path),
        # The name the candidate uploaded it as (e.g. "Ahmed_CV.pdf"),
        # falling back to the randomized on-disk name only for rows
        # written before original_filename existed.
        filename=profile.original_filename or resolved_path.name,
        media_type="application/octet-stream",
    )


@router.post("/job-description", response_model=JobDescriptionResponse)
async def add_job_description(
    payload: JobDescriptionRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    user_uuid = uuid.UUID(user_id)

    job = await save_as_active_record(
        db,
        JobDescription,
        user_uuid,
        lambda: JobDescription(
            user_id=user_uuid,
            job_title=payload.job_title,
            description_text=payload.description_text,
            is_active=True,
        ),
    )

    return JobDescriptionResponse(message="Job Description saved successfully", job_id=str(job.id))


@router.post("/preferences", response_model=InterviewPreferenceResponse)
async def save_interview_preferences(
    payload: InterviewPreferenceRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    user_uuid = uuid.UUID(user_id)

    pref = await save_as_active_record(
        db,
        InterviewPreference,
        user_uuid,
        lambda: InterviewPreference(
            user_id=user_uuid,
            company_name=payload.company_name,
            job_title=payload.job_title,
            language=payload.language,
            interview_date=payload.interview_date,
            is_active=True,
        ),
    )

    return InterviewPreferenceResponse(
        message="Interview preferences saved successfully", preference_id=str(pref.id)
    )
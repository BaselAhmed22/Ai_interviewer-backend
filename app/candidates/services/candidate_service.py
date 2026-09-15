# Shared "one active record per user" pattern used for CV profiles and
# job descriptions, plus the CV file-upload core used by
# POST /interviews/prepare/upload (see app/interviews/api/pipeline.py —
# the standalone /candidates/* endpoints that used to own this file
# handling were removed; this module is now purely an internal building
# block for the self-contained prepare flow).
import os
import uuid
from typing import Callable, Type, TypeVar

import aiofiles
from fastapi import HTTPException, UploadFile, status
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.candidates.models import CandidateProfile

ModelT = TypeVar("ModelT")

ALLOWED_CV_EXTENSIONS = (".pdf", ".docx")


async def deactivate_active_records(db: AsyncSession, model: Type[ModelT], user_id: uuid.UUID) -> None:
    await db.execute(
        update(model).where(model.user_id == user_id, model.is_active.is_(True)).values(is_active=False)
    )


async def save_as_active_record(
    db: AsyncSession, model: Type[ModelT], user_id: uuid.UUID, build_row: Callable[[], ModelT]
) -> ModelT:
    """Deactivate this user's current active row (if any) and insert a
    fresh one as the new active one. A partial unique index on
    (user_id) WHERE is_active enforces this at the DB level — two
    concurrent first-time saves for a user with no active row yet can
    both reach the INSERT before either commits, so retry once on the
    resulting IntegrityError. By the retry, the other request's row is
    already committed and visible, so deactivating it and inserting ours
    succeeds cleanly."""
    for attempt in range(2):
        await deactivate_active_records(db, model, user_id)
        row = build_row()
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if attempt == 1:
                raise
            continue
        await db.refresh(row)
        return row


async def save_uploaded_cv(db: AsyncSession, user_uuid: uuid.UUID, file: UploadFile) -> CandidateProfile:
    """Validates, streams to disk, and creates the CandidateProfile row as
    the user's new active one, for POST /interviews/prepare/upload. Does
    NOT extract CV text — the caller does that synchronously right after,
    since DocumentAgent needs raw_cv_text immediately, before returning."""
    if not file.filename or not file.filename.lower().endswith(ALLOWED_CV_EXTENSIONS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "unsupported_file_type", "message": "Only PDF and DOCX files are allowed."},
        )

    # Bounded chunks so an oversized upload is rejected before it's fully
    # buffered into memory.
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
    # Absolute, since FastAPI and the Celery worker are separate processes
    # with independent working directories.
    file_path = settings.UPLOAD_DIR / unique_filename

    try:
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "storage_error", "message": "Could not store the file. Please try again."},
        ) from exc

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
        # DB transaction failed after the file already landed on disk —
        # remove it instead of leaving an orphaned upload.
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise
    return profile

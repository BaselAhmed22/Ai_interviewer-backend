import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.core.security import get_current_user_id
from app.db.models.session import InterviewSession, SessionStatus
from app.services.livekit_service import livekit_service
from app.schemas.interview import InterviewStartRequest, InterviewStartResponse

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/start", response_model=InterviewStartResponse)
async def start_interview(
    payload: InterviewStartRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Join (or rejoin) the LiveKit room for an interview already created
    via POST /sessions/start. Verifies ownership and that it's still
    active, then hands back a fresh token for its room."""
    try:
        session_uuid = uuid.UUID(payload.interview_id)
    except ValueError:
        # A malformed ID reads as "no such interview," not a 422 — interviewId
        # is an opaque identifier from the frontend's point of view.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Interview not found."},
        )

    result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_uuid))
    session_obj = result.scalars().first()

    if not session_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Interview not found."},
        )
    if str(session_obj.user_id) != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Not your interview."},
        )
    if session_obj.status != SessionStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "session_not_active", "message": "This interview is not active."},
        )

    try:
        token = livekit_service.generate_token(
            room_name=session_obj.room_name,
            participant_identity=user_id,
        )
    except Exception as exc:
        logger.error("LiveKit token generation failed for interview %s: %s", session_uuid, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "livekit_unavailable", "message": "Could not generate a token. Please try again."},
        )

    return InterviewStartResponse(room_name=session_obj.room_name, token=token)

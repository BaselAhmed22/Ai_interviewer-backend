# app/api/v1/endpoints/interview.py
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
    """
    Join (or rejoin) the LiveKit room for an existing interview. The
    interview/session itself must already exist — created via
    POST /sessions/start, which is the step that actually sets up the
    InterviewSession row, generates its room_name, and dispatches the AI
    agent. This endpoint's job is narrower: verify the caller owns this
    exact interview and it's still active, then hand back a fresh
    LiveKit token for its room.
    """
    try:
        session_uuid = uuid.UUID(payload.interview_id)
    except ValueError:
        # Not a well-formed ID at all — same externally-visible outcome as
        # "no such interview" rather than a 422, since interviewId is an
        # opaque identifier from the frontend's point of view.
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
        # Same 403 "forbidden" shape used for ownership checks everywhere
        # else in this API (end_session, reconnect-token, session report)
        # — kept consistent rather than introducing a different
        # not-found-vs-forbidden convention for just this one endpoint.
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

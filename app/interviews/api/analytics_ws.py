import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from pydantic import ValidationError
from sqlalchemy import select

from app.interviews.schemas.metadata import FrameMetadataPayload, AlertMessage
from app.core.redis_service import redis_service
from app.core.security import decode_user_id
from app.core.database import AsyncSessionLocal
from app.interviews.models.session import InterviewSession, SessionStatus

router = APIRouter()
logger = logging.getLogger(__name__)

EYE_CONTACT_THRESHOLD = 10  # ~3 seconds of consecutive lost eye contact
POSTURE_THRESHOLD = 15      # ~4.5 seconds of consecutive slouching
SESSION_RECHECK_INTERVAL = 5.0   # seconds between re-validating the session is still IN_PROGRESS
MAX_FRAMES_PER_SECOND = 60       # generous headroom over any plausible client frame rate

class AlertEngine:

    async def process_metadata(self, payload: FrameMetadataPayload) -> list[AlertMessage]:
        alerts: list[AlertMessage] = []
        session_id = payload.session_id
        current_time = time.time()

        if not payload.eye_contact.is_looking_at_camera:
            count = await redis_service.increment_counter(f"session:{session_id}:eye_lost_count")
            if count >= EYE_CONTACT_THRESHOLD:
                alerts.append(
                    AlertMessage(
                        session_id=session_id,
                        alert_type="EYE_CONTACT_LOST",
                        message="Please maintain eye contact with the interviewer.",
                        timestamp=current_time,
                    )
                )
        else:
            await redis_service.reset_counter(f"session:{session_id}:eye_lost_count")

        if payload.posture.is_slouching:
            count = await redis_service.increment_counter(f"session:{session_id}:slouch_count")
            if count >= POSTURE_THRESHOLD:
                alerts.append(
                    AlertMessage(
                        session_id=session_id,
                        alert_type="BAD_POSTURE",
                        message="Try sitting up straight to improve your presence.",
                        timestamp=current_time,
                    )
                )
        else:
            await redis_service.reset_counter(f"session:{session_id}:slouch_count")

        return alerts


alert_engine = AlertEngine()


async def _session_belongs_to_user_and_active(session_id: str, user_id: str) -> bool:
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        return False

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(InterviewSession).where(InterviewSession.id == session_uuid))
        session_obj = result.scalars().first()

    if not session_obj:
        return False
    if str(session_obj.user_id) != user_id:
        return False
    if session_obj.status != SessionStatus.IN_PROGRESS:
        return False
    return True


@router.websocket("/ws/analytics/{session_id}")
async def analytics_websocket(websocket: WebSocket, session_id: str, token: str = Query(...)):
    try:
        user_id = decode_user_id(token)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if not await _session_belongs_to_user_and_active(session_id, user_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    last_session_check = time.time()
    window_start = time.time()
    frames_in_window = 0
    try:
        while True:
            # Re-check periodically, not just at connect — otherwise a
            # session ended from another tab/device would leave this
            # socket streaming into a session already COMPLETED in the DB.
            now = time.time()
            if now - last_session_check >= SESSION_RECHECK_INTERVAL:
                last_session_check = now
                if not await _session_belongs_to_user_and_active(session_id, user_id):
                    logger.info("Session %s no longer active — closing analytics socket.", session_id)
                    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                    return

            try:
                data_text = await asyncio.wait_for(
                    websocket.receive_text(), timeout=SESSION_RECHECK_INTERVAL
                )
            except asyncio.TimeoutError:
                continue  # nothing arrived; loop back around to the re-check above

            # Per-connection frame-rate cap — drop excess frames instead
            # of forwarding them, so a runaway sender can't hammer Redis.
            if now - window_start >= 1.0:
                window_start = now
                frames_in_window = 0
            frames_in_window += 1
            if frames_in_window > MAX_FRAMES_PER_SECOND:
                continue

            # Skip a malformed frame rather than let it fall through to the
            # outer except and kill the connection over one bad message.
            try:
                data_json = json.loads(data_text)
                data_json["session_id"] = session_id
                payload = FrameMetadataPayload(**data_json)
            except (json.JSONDecodeError, ValidationError) as exc:
                logger.warning("Discarding malformed analytics frame on session %s: %s", session_id, exc)
                continue

            alerts = await alert_engine.process_metadata(payload)

            for alert in alerts:
                await websocket.send_json(alert.model_dump(by_alias=True))

    except WebSocketDisconnect:
        logger.info("Client disconnected from WebSocket session: %s", session_id)
    except Exception as e:
        logger.error("WebSocket error on session %s: %s", session_id, e)
        await websocket.close()
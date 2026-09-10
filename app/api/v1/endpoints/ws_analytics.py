# Real-time WebSocket Endpoint — now also owns the alert-threshold logic
# that used to live in the standalone services/alert_engine.py (see the
# merge rationale in section 2 above: single consumer, no reuse elsewhere).
import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from pydantic import ValidationError
from sqlalchemy import select

from app.schemas.metadata import FrameMetadataPayload, AlertMessage
from app.services.redis_service import redis_service
from app.core.security import decode_user_id
from app.core.database import AsyncSessionLocal
from app.db.models.session import InterviewSession, SessionStatus

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
            # The ownership/IN_PROGRESS check only ran once, at connect
            # time. If the session is ended via POST /sessions/end/{id}
            # from another tab/device while this socket stays open, it
            # kept streaming and writing Redis counters for a session
            # that's already COMPLETED in the DB, with nothing to notice
            # or close it. Re-check periodically instead of only once.
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

            # Simple per-connection frame-rate cap — nothing bounded how
            # fast a client could push frames, so a runaway/buggy sender
            # could hammer Redis with no backpressure. Excess frames in
            # the current window are dropped silently rather than
            # forwarded to the alert engine.
            if now - window_start >= 1.0:
                window_start = now
                frames_in_window = 0
            frames_in_window += 1
            if frames_in_window > MAX_FRAMES_PER_SECOND:
                continue

            # A single malformed frame (bad JSON, a field out of range)
            # used to take down the whole connection via the outer
            # except below — forcing a full reconnect + re-auth handshake
            # mid-interview. Skip just the bad frame instead; only a real
            # connection-level failure should end the loop.
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
"""Persists and retrieves the live conversation log captured by the
LiveKit agent (see app/interviews/workers/livekit_agent.py and
app/interviews/agents/voice_agent.py). Two entry points with different homes: the
agent worker is a separate OS process with no FastAPI request to hang a
DB session off of, so save_transcript opens its own; get_transcript is
called from the FastAPI process, which already has one to pass in.
"""
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.interviews.models.transcript import InterviewTranscript

# LiveKit's ChatRole also includes "system"/"developer" (the interviewer's
# own instructions) — never spoken conversation, so only these two map to
# a transcript turn.
_SPEAKER_BY_ROLE = {"user": "candidate", "assistant": "agent"}


def build_turn(message: Any) -> dict | None:
    """Converts one LiveKit ChatMessage into our {speaker, text, timestamp}
    shape, or None if it's not a conversational turn (wrong role, or no
    text — e.g. a tool call)."""
    speaker = _SPEAKER_BY_ROLE.get(getattr(message, "role", None))
    if speaker is None:
        return None
    text = (getattr(message, "text_content", None) or "").strip()
    if not text:
        return None
    created_at = getattr(message, "created_at", None)
    timestamp = datetime.fromtimestamp(created_at, tz=timezone.utc) if created_at else datetime.now(timezone.utc)
    return {"speaker": speaker, "text": text, "timestamp": timestamp.isoformat()}


def build_turns(messages: list[Any]) -> list[dict]:
    """Converts a LiveKit AgentSession's chat history (session.history.
    messages(), a list of ChatMessage) into our {speaker, text, timestamp}
    shape, dropping non-conversational roles and empty turns."""
    turns = [build_turn(message) for message in messages]
    return [turn for turn in turns if turn is not None]


async def save_transcript(session_id: uuid.UUID, turns: list[dict]) -> None:
    """Upserts the full transcript for one interview session. Called from
    the LiveKit agent process — opens its own short-lived DB session."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(InterviewTranscript).where(InterviewTranscript.session_id == session_id))
        row = result.scalars().first()
        if row:
            row.turns = turns
        else:
            db.add(InterviewTranscript(session_id=session_id, turns=turns))
        await db.commit()


async def get_transcript(db: AsyncSession, session_id: uuid.UUID) -> InterviewTranscript | None:
    result = await db.execute(select(InterviewTranscript).where(InterviewTranscript.session_id == session_id))
    return result.scalars().first()

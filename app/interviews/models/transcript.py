import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, JSON, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class InterviewTranscript(Base):
    """The full candidate/agent conversation log for one interview,
    captured live by the LiveKit agent (see app/interviews/workers/livekit_agent.py's
    "conversation_item_added" hook) and written here once the session
    ends (InterviewerAgent.on_exit, app/interviews/agents/voice_agent.py). This is
    the input payload the real evaluator will eventually score against —
    see app/ai_evaluator/evaluation_agent.py."""

    __tablename__ = "interview_transcripts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_sessions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # Chronological turns: [{"speaker": "candidate"|"agent", "text": str,
    # "timestamp": ISO 8601}]. "system"/"developer" chat-context entries
    # (the interviewer's own instructions, not spoken conversation) are
    # filtered out before this is written — see
    # app.interviews.services.transcript_service.build_turns.
    turns: Mapped[Optional[list[dict]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    session = relationship("InterviewSession", back_populates="transcript")

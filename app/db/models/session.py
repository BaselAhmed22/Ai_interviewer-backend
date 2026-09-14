import enum
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, Enum, ForeignKey, Index, JSON, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

class SessionStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class InterviewSession(Base):
    __tablename__ = "interview_sessions"
    __table_args__ = (
        # The real guarantee behind "one active session per user" — the
        # app-level check in start_session has a race window a concurrent
        # insert can hit; this index turns that into a 409, not a bypass.
        Index(
            "ux_interview_sessions_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'IN_PROGRESS'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_name: Mapped[str] = mapped_column(nullable=False, unique=True)
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus), default=SessionStatus.IN_PROGRESS)
    failure_reason: Mapped[str | None] = mapped_column(nullable=True)
    # The prepared question list, when this session went through the
    # multi-agent pipeline (POST /interviews/start) — null for the plain
    # /sessions/start path, which has no prepared questions.
    questions: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    # DocumentAgent's CV-vs-job analysis (see
    # app.schemas.agent_context.CandidateSummary), persisted so it
    # survives past the Redis pipeline context's TTL. Null, same as `questions`.
    candidate_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    report = relationship("InterviewReport", back_populates="session", uselist=False)
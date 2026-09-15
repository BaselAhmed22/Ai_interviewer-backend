import enum
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, Enum, ForeignKey, Index, JSON, String, func, text
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
        # app-level check in ensure_ready_to_start has a race window a
        # concurrent insert can hit; this index turns that into a 409, not a bypass.
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
    # The prepared question list from the multi-agent pipeline
    # (POST /interviews/start) — nullable for historical rows created by
    # the since-removed direct /sessions/start path, which had none. Each
    # item is a GeneratedQuestion dump: {question, category, difficulty}.
    questions: Mapped[Optional[list[dict]]] = mapped_column(JSON, nullable=True)
    # DocumentAgent's CV-vs-job analysis (see
    # app.interviews.schemas.agent_context.CandidateSummary), persisted so it
    # survives past the Redis pipeline context's TTL. Null, same as `questions`.
    candidate_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Candidate's chosen (or custom) Simli avatar face, set at
    # POST /interviews/prepare and carried into the LiveKit agent's
    # dispatch metadata. Null falls back to SIMLI_FACE_ID in the agent's env.
    simli_face_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Snapshotted at POST /interviews/start from the JobDescription /
    # InterviewPreference active at that moment — same reasoning as
    # questions/candidate_summary above: a candidate's active job
    # description or company can change after the fact, and GET /sessions'
    # dashboard cards (job_position/companyName) need what THIS interview
    # was actually for, not whatever happens to be active now. Nullable
    # for historical rows predating this snapshot and because
    # company_name has no required-preference flow (InterviewPreference
    # is optional).
    job_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    report = relationship("InterviewReport", back_populates="session", uselist=False)
    transcript = relationship("InterviewTranscript", back_populates="session", uselist=False)
import uuid
from typing import Optional, Any, Dict
from sqlalchemy import Float, ForeignKey, JSON, Boolean, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class InterviewReport(Base):
    __tablename__ = "interview_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_sessions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    overall_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Real, transcript-based Gemini evaluation (see
    # app.ai_evaluator.evaluation_agent.EvaluationAgent) — populated by
    # generate_final_interview_report (app/workers/tasks.py) going forward.
    technical_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    problem_solving_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    communication_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    strengths: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    weaknesses: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    recommendation: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Legacy fields from the old fixed-sample placeholder evaluator —
    # kept, nullable, so historical rows created before this migration
    # still load; no longer written by generate_final_interview_report.
    eye_contact_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    posture_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    speech_clarity_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    feedback_summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    detailed_metrics: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    session = relationship("InterviewSession", back_populates="report")
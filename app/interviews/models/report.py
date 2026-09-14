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
    eye_contact_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    posture_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    speech_clarity_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    feedback_summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    detailed_metrics: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    session = relationship("InterviewSession", back_populates="report")
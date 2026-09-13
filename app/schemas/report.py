from typing import Optional, Any, Dict
from pydantic import field_validator
from app.schemas.base import CamelModel

class ReportBase(CamelModel):
    session_id: str
    overall_score: Optional[float] = None
    eye_contact_score: Optional[float] = None
    posture_score: Optional[float] = None
    speech_clarity_score: Optional[float] = None
    feedback_summary: Optional[str] = None
    detailed_metrics: Optional[Dict[str, Any]] = None
    is_placeholder: bool = True

class ReportResponse(ReportBase):
    id: str

    # InterviewReport.id / .session_id are UUID columns — model_validate()
    # reads them straight off the ORM object, so they arrive here as
    # uuid.UUID, not str.
    @field_validator("id", "session_id", mode="before")
    @classmethod
    def _stringify_uuid(cls, value):
        return str(value) if value is not None else value
from typing import Optional, Any, Dict
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

class ReportCreate(ReportBase):
    id: str

class ReportUpdate(CamelModel):
    overall_score: Optional[float] = None
    eye_contact_score: Optional[float] = None
    posture_score: Optional[float] = None
    speech_clarity_score: Optional[float] = None
    feedback_summary: Optional[str] = None
    detailed_metrics: Optional[Dict[str, Any]] = None
    is_placeholder: Optional[bool] = None

class ReportResponse(ReportBase):
    id: str
from datetime import datetime
from typing import Optional
from app.schemas.base import CamelModel, NoControlCharsMixin
from app.schemas.report import ReportResponse

# --- Session lifecycle schemas ---
class SessionResponse(CamelModel):
    id: str
    user_id: str
    room_name: str
    status: str

class SessionListItem(CamelModel):
    id: str
    room_name: str
    status: str
    created_at: datetime

class CandidateSummaryResponse(CamelModel):
    """DocumentAgent's CV-vs-job analysis, as persisted onto the session
    row (see app.schemas.agent_context.CandidateSummary — the internal,
    non-camelCase version this is read from)."""
    headline: Optional[str] = None
    key_skills: list[str] = []
    experience: list[str] = []
    projects: list[str] = []
    required_skills: list[str] = []
    matching_skills: list[str] = []
    missing_skills: list[str] = []
    profile: Optional[str] = None

class InterviewDetailResponse(CamelModel):
    id: str
    user_id: str
    room_name: str
    status: str
    created_at: datetime
    updated_at: datetime
    questions: Optional[list[str]] = None
    candidate_summary: Optional[CandidateSummaryResponse] = None
    failure_reason: Optional[str] = None
    report: Optional[ReportResponse] = None
    # Convenience top-level read of report.detailed_metrics["transcript"],
    # if the evaluation pipeline ever populates one there — null until it
    # does (no live transcript capture exists yet).
    transcript: Optional[str] = None

class SessionStartResponse(CamelModel):
    id: str
    user_id: str
    room_name: str
    status: str
    livekit_token: str
    livekit_server_url: str
    interviewer_name: str = "Aria"
    interviewer_title: str
    total_questions: int = 5

class EndSessionResponse(CamelModel):
    message: str
    session_id: str
    task_id: Optional[str] = None


# --- LiveKit reconnect schemas (formerly schemas/livekit.py) ---
class ReconnectTokenRequest(NoControlCharsMixin, CamelModel):
    participant_name: Optional[str] = None

class ReconnectTokenResponse(CamelModel):
    token: str
    server_url: str
    room_name: str


class AgentStatusResponse(CamelModel):
    # "unknown" means no status has ever been reported for this room yet
    # (agent hasn't joined) or the last report is stale (agent process
    # crashed without a clean exit) — see redis_service.set_agent_status.
    status: str
    detail: Optional[str] = None
    updated_at: Optional[str] = None
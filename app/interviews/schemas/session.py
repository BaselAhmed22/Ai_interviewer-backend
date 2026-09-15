from datetime import datetime
from typing import Optional
from pydantic import field_validator
from app.core.schemas_base import CamelModel, NoControlCharsMixin
from app.interviews.schemas.report import EvaluationReportResponse

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
    # Snapshotted on the session row at /interviews/start (see
    # InterviewSession.job_title's docstring) — null for sessions that
    # predate this field, or that never resolved a job/company at start
    # time. overall_score is null until the report is generated
    # (COMPLETED with no report yet, IN_PROGRESS, or FAILED) — see
    # GET /sessions/{id}/report for the full pending/failed distinction;
    # this list endpoint only needs the number, not the state machine.
    job_position: Optional[str] = None
    company_name: Optional[str] = None
    overall_score: Optional[float] = None

class CandidateSummaryResponse(CamelModel):
    """DocumentAgent's CV-vs-job analysis, as persisted onto the session
    row (see app.interviews.schemas.agent_context.CandidateSummary — the internal,
    non-camelCase version this is read from)."""
    headline: Optional[str] = None
    key_skills: list[str] = []
    experience: list[str] = []
    projects: list[str] = []
    required_skills: list[str] = []
    matching_skills: list[str] = []
    missing_skills: list[str] = []
    profile: Optional[str] = None

class GeneratedQuestionResponse(CamelModel):
    """See app.interviews.schemas.agent_context.GeneratedQuestion — the internal,
    non-camelCase version this is read from."""
    question: str
    category: str = "General"
    difficulty: str = "medium"

class TranscriptTurnResponse(CamelModel):
    """One candidate/agent turn — see app.interviews.models.transcript.InterviewTranscript."""
    speaker: str  # "candidate" | "agent"
    text: str
    timestamp: datetime

class TranscriptResponse(CamelModel):
    session_id: str
    turns: list[TranscriptTurnResponse]
    updated_at: datetime

class InterviewDetailResponse(CamelModel):
    id: str
    user_id: str
    room_name: str
    status: str
    created_at: datetime
    updated_at: datetime
    questions: Optional[list[GeneratedQuestionResponse]] = None
    candidate_summary: Optional[CandidateSummaryResponse] = None
    simli_face_id: Optional[str] = None
    failure_reason: Optional[str] = None
    report: Optional[EvaluationReportResponse] = None
    # Convenience top-level read of the full transcript (see
    # app.interviews.services.transcript_service) — null until the interview ends
    # and InterviewerAgent.on_exit() writes it. Use
    # GET /sessions/{id}/transcript directly for just this.
    transcript: Optional[list[TranscriptTurnResponse]] = None

    @field_validator("questions", mode="before")
    @classmethod
    def _coerce_legacy_question_strings(cls, value):
        # Sessions created before question-difficulty tagging stored
        # questions as bare strings — normalize those into the current
        # {question, category, difficulty} shape instead of failing
        # response validation on old rows.
        if not value:
            return value
        return [
            {"question": item, "category": "General", "difficulty": "medium"} if isinstance(item, str) else item
            for item in value
        ]

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
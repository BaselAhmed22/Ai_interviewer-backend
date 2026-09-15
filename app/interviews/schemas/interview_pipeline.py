# Request/response contracts for the multi-agent pipeline endpoints
# (app/interviews/api/pipeline.py).
from typing import Optional

from pydantic import Field

from app.core.schemas_base import CamelModel, NoControlCharsMixin


class PrepareInterviewRequest(NoControlCharsMixin, CamelModel):
    """Fully self-contained: no separate upload-cv/job-description calls
    needed first. cv_text is plain, already-extracted CV text (a client
    that has its own PDF/DOCX parsing, or a candidate pasting text
    directly) — POST /interviews/prepare/upload is the file-upload
    equivalent of this same endpoint for a raw PDF/DOCX."""

    cv_text: str = Field(min_length=1, max_length=50000)
    job_title: str = Field(min_length=1, max_length=255)
    job_description: str = Field(min_length=1, max_length=20000)
    # Optional: shown on GET /sessions' dashboard cards (companyName) —
    # see InterviewSession.company_name's docstring. No dedicated
    # "preferences" step anymore; this is the only place it's captured.
    company_name: Optional[str] = Field(default=None, max_length=255)

    # Bypasses the interview-prep-cache lookup (see
    # interview_pipeline._fingerprint) to force a fresh Gemini call even
    # when an identical CV+job fingerprint is already cached. The fresh
    # result still overwrites the cache afterward. Defaults to False since
    # almost every caller wants the cache's quota savings.
    force_regenerate: bool = False

    number_of_questions: int = Field(default=5, ge=3, le=10)


class PrepareInterviewResponse(CamelModel):
    preparation_id: str
    questions_count: int
    candidate_headline: Optional[str] = None
    remaining_attempts: int
    # True if candidate_summary/questions came from interview-prep-cache
    # instead of a fresh Gemini call — a low-latency signal, not an error
    # condition; see interview_pipeline.py's module docstring.
    from_cache: bool = False


class StartPipelineRequest(NoControlCharsMixin, CamelModel):
    preparation_id: str = Field(min_length=1, max_length=64)


class StartPipelineResponse(CamelModel):
    session_id: str
    room_name: str
    livekit_token: str
    livekit_server_url: str


class EvaluateInterviewRequest(NoControlCharsMixin, CamelModel):
    session_id: str = Field(min_length=1, max_length=64)


class EvaluateInterviewResponse(CamelModel):
    message: str
    session_id: str
    task_id: Optional[str] = None

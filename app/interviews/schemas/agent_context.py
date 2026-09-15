# Shared state passed between pipeline stages (app/interviews/agents/*) and persisted
# by InterviewPipelineManager (app/interviews/services/interview_pipeline.py). Plain
# pydantic BaseModel, not CamelModel — this is internal Redis-serialized
# state, not an HTTP request/response contract.
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class PipelineStage(str, Enum):
    DOCUMENT = "document"
    QUESTIONNAIRE = "questionnaire"
    CONTROLLER = "controller"
    VOICE = "voice"
    EVALUATION = "evaluation"
    COMPLETED = "completed"


class CandidateSummary(BaseModel):
    """DocumentAgent's output — real Gemini-based CV/job analysis (see
    app/interviews/agents/document_agent.py)."""

    headline: Optional[str] = None
    key_skills: list[str] = []
    raw_cv_excerpt: Optional[str] = None

    # required_skills lives here rather than on a separate job-side schema
    # since it comes from the same CV-vs-job-description Gemini call.
    experience: list[str] = []
    projects: list[str] = []
    required_skills: list[str] = []
    matching_skills: list[str] = []
    missing_skills: list[str] = []
    profile: Optional[str] = None


class GeneratedQuestion(BaseModel):
    """One QuestionnaireAgent output. difficulty is assigned in Python
    (see questionnaire_agent._balanced_difficulties), not by Gemini —
    keeps the easy/medium/hard mix consistent regardless of what the
    model itself would produce."""

    question: str
    category: str = "General"
    difficulty: str = "medium"  # "easy" | "medium" | "hard"


class AgentContext(BaseModel):
    """Working state of one interview pipeline run, from `prepare` through
    `evaluate`. Persisted to Redis, keyed by preparation_id — ephemeral;
    the durable record lives in InterviewSession / InterviewReport."""

    preparation_id: str
    user_id: str
    candidate_profile_id: str
    job_description_id: str
    stage: PipelineStage

    candidate_summary: Optional[CandidateSummary] = None
    questions: list[GeneratedQuestion] = []
    current_question_index: int = 0

    # True when candidate_summary/questions were served from
    # interview-prep-cache (see interview_pipeline._fingerprint) instead of
    # spending fresh Gemini calls — surfaced to the client as
    # PrepareInterviewResponse.from_cache.
    from_cache: bool = False

    # Free-text company name captured directly on the prepare request
    # (see PrepareInterviewRequest.company_name) — snapshotted onto the
    # session row at /interviews/start for GET /sessions' dashboard cards.
    company_name: Optional[str] = None

    session_id: Optional[str] = None

    created_at: datetime
    updated_at: datetime

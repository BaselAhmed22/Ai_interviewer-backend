# Shared state passed between pipeline stages (app/agents/*) and persisted
# by InterviewPipelineManager (app/services/interview_pipeline.py). Plain
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
    app/agents/document_agent.py)."""

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
    questions: list[str] = []
    current_question_index: int = 0

    session_id: Optional[str] = None

    created_at: datetime
    updated_at: datetime

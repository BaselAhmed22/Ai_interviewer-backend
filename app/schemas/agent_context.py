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
    """DocumentAgent's output — a placeholder shape today (see
    app/agents/document_agent.py). The AI team should extend this with
    whatever structured fields real CV analysis produces, as long as
    QuestionnaireAgent's input contract is updated alongside it."""

    headline: Optional[str] = None
    key_skills: list[str] = []
    raw_cv_excerpt: Optional[str] = None


class AgentContext(BaseModel):
    """The working state of one interview pipeline run, from `prepare`
    through `evaluate`. Persisted to Redis (see
    redis_service.save_pipeline_context/get_pipeline_context) keyed by
    preparation_id — ephemeral working memory for one interview, not a
    permanent record. The durable record of the interview itself lives in
    the existing InterviewSession / InterviewReport tables, unaffected by
    this context expiring."""

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

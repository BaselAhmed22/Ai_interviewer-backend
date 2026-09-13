# app/schemas/interview_pipeline.py — request/response contracts for the
# multi-agent pipeline endpoints (app/api/v1/endpoints/interviews.py).
from typing import Optional

from pydantic import Field

from app.schemas.base import CamelModel, NoControlCharsMixin


class PrepareInterviewResponse(CamelModel):
    preparation_id: str
    questions_count: int
    candidate_headline: Optional[str] = None


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

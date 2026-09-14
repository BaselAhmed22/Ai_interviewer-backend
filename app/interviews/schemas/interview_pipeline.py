# Request/response contracts for the multi-agent pipeline endpoints
# (app/interviews/api/pipeline.py).
from typing import Optional

from pydantic import Field

from app.core.schemas_base import CamelModel, NoControlCharsMixin


class PrepareInterviewRequest(NoControlCharsMixin, CamelModel):
    # Simli avatar face for this interview — a preset id from the app's
    # picker, or a custom one the candidate uploaded to their own Simli
    # account. Left as a plain validated string (not an enum) since Simli
    # face ids are user/account-specific, not a fixed list this backend
    # owns. Falls back to SIMLI_FACE_ID in the agent's env if omitted.
    simli_face_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")


class PrepareInterviewResponse(CamelModel):
    preparation_id: str
    questions_count: int
    candidate_headline: Optional[str] = None
    remaining_attempts: int


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

from pydantic import Field
from app.core.schemas_base import CamelModel, NoControlCharsMixin


class InterviewStartRequest(NoControlCharsMixin, CamelModel):
    # Maps onto an existing InterviewSession.id — created via
    # POST /sessions/start, not by this endpoint.
    interview_id: str = Field(min_length=1, max_length=64)


class InterviewStartResponse(CamelModel):
    room_name: str
    token: str

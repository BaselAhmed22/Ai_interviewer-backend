from pydantic import Field
from app.schemas.base import CamelModel, NoControlCharsMixin


class InterviewStartRequest(NoControlCharsMixin, CamelModel):
    # Maps onto an existing InterviewSession.id (a UUID) — this endpoint
    # joins/rejoins an interview that already exists (created via
    # POST /sessions/start), it doesn't create one from an interviewId
    # alone, since a session also requires an active CV and job
    # description to exist first.
    interview_id: str = Field(min_length=1, max_length=64)


class InterviewStartResponse(CamelModel):
    room_name: str
    token: str

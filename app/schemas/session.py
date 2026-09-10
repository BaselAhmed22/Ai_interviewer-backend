from datetime import datetime
from typing import Optional
from app.schemas.base import CamelModel, NoControlCharsMixin

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
from datetime import datetime
from typing import Optional

from app.core.schemas_base import CamelModel


class AdminUserListItem(CamelModel):
    id: str
    email: str
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_approved: bool
    interview_attempts_used: int
    created_at: datetime


class SystemSettingsResponse(CamelModel):
    require_admin_approval: bool
    updated_at: datetime


class UpdateSystemSettingsRequest(CamelModel):
    require_admin_approval: bool

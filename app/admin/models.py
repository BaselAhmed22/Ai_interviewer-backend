from datetime import datetime

from sqlalchemy import Boolean, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Singleton row (always id=1) — see app.admin.services.system_settings_service.
SETTINGS_ROW_ID = 1


class SystemSettings(Base):
    """Runtime-editable, admin-facing settings — as opposed to
    app.core.config.Settings (process-level, .env-driven, needs a restart
    to change). PATCH /api/v1/admin/settings edits this table so an admin
    can flip a feature flag without redeploying."""

    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    # See app.core.deps.get_current_active_user / app.interviews.services.session_service
    # .ensure_ready_to_start — when False, a User.is_approved of False is
    # never enforced; when True, it's the existing pending-approval gate.
    require_admin_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

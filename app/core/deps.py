import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.core.security import get_current_user_id
from app.auth.models import User, is_admin
from app.admin.services import system_settings_service


async def get_current_active_user(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Loads the caller's User row and, only while the
    require_admin_approval feature flag is on (see
    app.admin.services.system_settings_service), blocks accounts still pending
    admin approval from any interview-related endpoint. Use this instead
    of the bare get_current_user_id wherever approval status matters."""
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "User not found."},
        )

    settings_row = await system_settings_service.get_settings(db)
    if settings_row.require_admin_approval and not user.is_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "account_pending_approval",
                "message": "Your account is pending admin approval. Please check back shortly.",
            },
        )
    return user


async def get_current_admin_user(user: User = Depends(get_current_active_user)) -> User:
    if not is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "admin_required", "message": "Admin access required."},
        )
    return user

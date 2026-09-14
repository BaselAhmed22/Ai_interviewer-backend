import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.core.deps import get_current_admin_user
from app.auth.models import User
from app.admin.schemas import AdminUserListItem, SystemSettingsResponse, UpdateSystemSettingsRequest
from app.auth.schemas import MessageResponse
from app.admin.services import system_settings_service

router = APIRouter()


def _to_list_item(user: User) -> AdminUserListItem:
    return AdminUserListItem(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_approved=user.is_approved,
        interview_attempts_used=user.interview_attempts_used,
        created_at=user.created_at,
    )


@router.get("/users", response_model=list[AdminUserListItem])
async def list_users(
    pending_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    query = select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    if pending_only:
        query = query.where(User.is_approved.is_(False))

    result = await db.execute(query)
    return [_to_list_item(user) for user in result.scalars().all()]


@router.post("/users/{user_id}/approve", response_model=MessageResponse)
async def approve_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "User not found."})

    user.is_approved = True
    await db.commit()
    return MessageResponse(message=f"{user.email} approved.")


@router.post("/users/{user_id}/revoke", response_model=MessageResponse)
async def revoke_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin_user),
):
    """Undo an approval — the account is blocked from interview-related
    endpoints again until re-approved."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "User not found."})

    if user.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "cannot_revoke_self", "message": "You cannot revoke your own approval."},
        )

    user.is_approved = False
    await db.commit()
    return MessageResponse(message=f"{user.email} revoked.")


@router.get("/settings", response_model=SystemSettingsResponse)
async def get_system_settings(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    row = await system_settings_service.get_settings(db)
    return SystemSettingsResponse(require_admin_approval=row.require_admin_approval, updated_at=row.updated_at)


@router.patch("/settings", response_model=SystemSettingsResponse)
async def update_system_settings(
    payload: UpdateSystemSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    """Flips the require_admin_approval feature flag at runtime — no
    restart needed. When off, User.is_approved is never checked (see
    app.core.deps.get_current_active_user and
    app.interviews.services.session_service.ensure_ready_to_start); the 3-attempt
    trial cap is a separate, unrelated check and is unaffected either way."""
    row = await system_settings_service.set_require_admin_approval(db, payload.require_admin_approval)
    return SystemSettingsResponse(require_admin_approval=row.require_admin_approval, updated_at=row.updated_at)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.admin.models import SETTINGS_ROW_ID, SystemSettings


async def get_settings(db: AsyncSession) -> SystemSettings:
    """The one system_settings row, seeded by migration 0015. Self-heals
    if it's somehow missing (manual DB edit, etc.) rather than raising —
    this runs on every authenticated request via get_current_active_user,
    so a missing row must not be able to break every login."""
    result = await db.execute(select(SystemSettings).where(SystemSettings.id == SETTINGS_ROW_ID))
    row = result.scalars().first()
    if row is None:
        row = SystemSettings(id=SETTINGS_ROW_ID, require_admin_approval=False)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def set_require_admin_approval(db: AsyncSession, value: bool) -> SystemSettings:
    row = await get_settings(db)
    row.require_admin_approval = value
    await db.commit()
    await db.refresh(row)
    return row

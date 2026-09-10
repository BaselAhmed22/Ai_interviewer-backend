# Small repository-style helper for the "one active record per user"
# pattern that candidate.py previously repeated inline, identically, for
# CV profiles, job descriptions, and interview preferences.
import uuid
from typing import Type, TypeVar

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT")


async def deactivate_active_records(db: AsyncSession, model: Type[ModelT], user_id: uuid.UUID) -> None:
    await db.execute(
        update(model).where(model.user_id == user_id, model.is_active.is_(True)).values(is_active=False)
    )

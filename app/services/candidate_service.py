# Small repository-style helper for the "one active record per user"
# pattern that candidate.py previously repeated inline, identically, for
# CV profiles, job descriptions, and interview preferences.
import uuid
from typing import Callable, Type, TypeVar

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT")


async def deactivate_active_records(db: AsyncSession, model: Type[ModelT], user_id: uuid.UUID) -> None:
    await db.execute(
        update(model).where(model.user_id == user_id, model.is_active.is_(True)).values(is_active=False)
    )


async def save_as_active_record(
    db: AsyncSession, model: Type[ModelT], user_id: uuid.UUID, build_row: Callable[[], ModelT]
) -> ModelT:
    """Deactivate this user's current active row (if any) and insert a
    fresh one as the new active one. A partial unique index on
    (user_id) WHERE is_active enforces this at the DB level — two
    concurrent first-time saves for a user with no active row yet can
    both reach the INSERT before either commits, so retry once on the
    resulting IntegrityError. By the retry, the other request's row is
    already committed and visible, so deactivating it and inserting ours
    succeeds cleanly."""
    for attempt in range(2):
        await deactivate_active_records(db, model, user_id)
        row = build_row()
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if attempt == 1:
                raise
            continue
        await db.refresh(row)
        return row

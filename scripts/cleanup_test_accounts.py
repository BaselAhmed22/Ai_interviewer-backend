"""
Purges mock/test accounts (email ending in @example.com) and everything
attached to them — CV profiles, job descriptions, interview preferences,
sessions, reports, refresh tokens — via each table's existing
ON DELETE CASCADE foreign key (see app/db/models/*.py), so deleting the
User row is enough; nothing here touches those tables directly.

Admins are never deleted, even if their email happens to end in
@example.com (e.g. a bootstrap admin created via ADMIN_EMAILS during
testing) — only "keep real admins and registered users intact" accounts
matching both conditions (test-looking email AND not an admin) are removed.

Usage:
    python -m scripts.cleanup_test_accounts            # dry run (default)
    python -m scripts.cleanup_test_accounts --execute   # actually delete
"""
import argparse
import asyncio

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, engine
from app.auth.models import User

TEST_EMAIL_SUFFIX = "@example.com"


def _test_account_filter():
    # NULL role counts as not-admin, but SQL's `NULL != 'admin'` evaluates
    # to NULL (not True), so it has to be spelled out with is_(None)
    # rather than a bare `User.role != "admin"`.
    return (
        User.email.ilike(f"%{TEST_EMAIL_SUFFIX}"),
        or_(User.role.is_(None), User.role != "admin"),
    )


async def find_test_accounts(db: AsyncSession) -> list[User]:
    """Read-only: the accounts a purge would remove."""
    result = await db.execute(select(User).where(*_test_account_filter()))
    return list(result.scalars().all())


async def purge_test_accounts(db: AsyncSession) -> int:
    """Deletes matching User rows and returns how many were removed.
    Related rows cascade automatically at the database level."""
    result = await db.execute(delete(User).where(*_test_account_filter()))
    await db.commit()
    return result.rowcount


async def _main(execute: bool) -> None:
    try:
        async with AsyncSessionLocal() as db:
            accounts = await find_test_accounts(db)

            if not accounts:
                print("No mock/test accounts found — nothing to do.")
                return

            print(f"Found {len(accounts)} mock/test account(s) ending in '{TEST_EMAIL_SUFFIX}':")
            for user in accounts:
                print(f"  {user.email}  (id={user.id}, created_at={user.created_at})")

            if not execute:
                print(
                    "\nDry run — no changes made. Re-run with --execute to delete these "
                    "accounts and everything attached to them (CV profiles, job "
                    "descriptions, interview preferences, sessions, reports, refresh tokens)."
                )
                return

            deleted = await purge_test_accounts(db)
            print(f"\nDeleted {deleted} account(s) and all associated data.")
    finally:
        # In the same event loop as everything above — disposing via a
        # second, separate asyncio.run() races the first loop's own
        # connection cleanup and prints a harmless but scary traceback.
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete matching accounts. Without this flag, only lists what would be deleted.",
    )
    args = parser.parse_args()

    asyncio.run(_main(args.execute))

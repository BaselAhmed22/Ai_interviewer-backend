# Refresh-token rotation with token families. A family is every refresh
# token descended from one login, sharing a family_id; logout and reuse
# detection both revoke the whole family, not just one token. Only a
# SHA-256 hash of the raw token is ever persisted.
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.auth.models import RefreshToken, RefreshTokenStatus
from app.auth.models import User
from app.auth.services import email_service
from app.core.redis_service import redis_service

logger = logging.getLogger(__name__)


@dataclass
class IssuedToken:
    raw_token: str
    user_id: uuid.UUID
    family_id: uuid.UUID
    expires_at: datetime


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _truncate_user_agent(user_agent: Optional[str]) -> Optional[str]:
    return (user_agent or "")[:512] or None


async def issue_new_family(
    db: AsyncSession, user_id: uuid.UUID, ip_address: Optional[str], user_agent: Optional[str]
) -> IssuedToken:
    """Starts a new token family. Called on register/login/Google sign-in."""
    now = datetime.now(timezone.utc)
    family_id = uuid.uuid4()
    expires_at = now + timedelta(days=settings.REFRESH_TOKEN_ABSOLUTE_EXPIRE_DAYS)
    raw_token = secrets.token_urlsafe(64)

    row = RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=_hash_token(raw_token),
        family_id=family_id,
        family_created_at=now,
        status=RefreshTokenStatus.ACTIVE,
        ip_address=ip_address,
        user_agent=_truncate_user_agent(user_agent),
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()

    return IssuedToken(raw_token=raw_token, user_id=user_id, family_id=family_id, expires_at=expires_at)


async def _revoke_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.family_id == family_id, RefreshToken.status == RefreshTokenStatus.ACTIVE
        )
    )
    for row in result.scalars().all():
        row.status = RefreshTokenStatus.REVOKED
        row.revoked_at = now
    await db.commit()


async def revoke_family_by_token(db: AsyncSession, raw_token: str) -> None:
    """Logout: revoke every token in the presented token's family."""
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == _hash_token(raw_token)))
    row = result.scalars().first()
    if not row:
        return

    now = datetime.now(timezone.utc)
    family_result = await db.execute(select(RefreshToken).where(RefreshToken.family_id == row.family_id))
    for member in family_result.scalars().all():
        if member.status != RefreshTokenStatus.REVOKED:
            member.status = RefreshTokenStatus.REVOKED
            member.revoked_at = now
    await db.commit()


async def _handle_reuse_attack(db: AsyncSession, reused_row: RefreshToken) -> None:
    logger.error(
        "Refresh token reuse detected for user %s, family %s — revoking the entire family.",
        reused_row.user_id,
        reused_row.family_id,
    )
    await _revoke_family(db, reused_row.family_id)

    user_result = await db.execute(select(User).where(User.id == reused_row.user_id))
    user = user_result.scalars().first()
    if user:
        try:
            await email_service.send_security_alert_email(
                user.email,
                "We detected that one of your login sessions was used in a way that looks like it was "
                "copied or intercepted. As a precaution, we've signed that session out everywhere. "
                "If this wasn't you, please change your password.",
            )
        except Exception as exc:
            logger.error("Failed to send reuse-attack security alert to %s: %s", user.email, exc)


async def rotate_refresh_token(
    db: AsyncSession, raw_token: str, ip_address: Optional[str], user_agent: Optional[str]
) -> IssuedToken:
    """Core of POST /auth/refresh. Raises 401 on any invalid, expired, or
    reused token — the caller should treat that as "log in again", not retry."""
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == _hash_token(raw_token)))
    row = result.scalars().first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "Invalid refresh token."},
        )

    now = datetime.now(timezone.utc)

    if now > row.expires_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "session_expired", "message": "Your session has expired. Please log in again."},
        )

    if row.status == RefreshTokenStatus.REVOKED:
        grace_deadline = (row.revoked_at or now) + timedelta(seconds=settings.REFRESH_TOKEN_GRACE_PERIOD_SECONDS)
        if now <= grace_deadline:
            # Two near-simultaneous refresh calls — since raw tokens are
            # never persisted, this cache is the only way to hand the
            # second request the same replacement the first one got.
            try:
                cached = await redis_service.get_cached_refresh_rotation(row.token_hash)
            except RedisError:
                cached = None
                logger.warning("Redis unavailable — cannot confirm grace-period race for family %s.", row.family_id)
            if cached:
                data = json.loads(cached)
                logger.info(
                    "Refresh token reused within grace period for family %s — treating as a race, not an attack.",
                    row.family_id,
                )
                return IssuedToken(
                    raw_token=data["raw_token"],
                    user_id=uuid.UUID(data["user_id"]),
                    family_id=uuid.UUID(data["family_id"]),
                    expires_at=datetime.fromisoformat(data["expires_at"]),
                )

        await _handle_reuse_attack(db, row)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "token_reuse_detected",
                "message": "This session was invalidated for security reasons. Please log in again.",
            },
        )

    new_raw_token = secrets.token_urlsafe(64)
    new_row = RefreshToken(
        id=uuid.uuid4(),
        user_id=row.user_id,
        token_hash=_hash_token(new_raw_token),
        family_id=row.family_id,
        family_created_at=row.family_created_at,
        status=RefreshTokenStatus.ACTIVE,
        ip_address=ip_address,
        user_agent=_truncate_user_agent(user_agent),
        expires_at=row.expires_at,
    )
    db.add(new_row)
    await db.flush()

    old_token_hash = row.token_hash
    row.status = RefreshTokenStatus.REVOKED
    row.revoked_at = now
    row.replaced_by_id = new_row.id
    await db.commit()

    issued = IssuedToken(raw_token=new_raw_token, user_id=row.user_id, family_id=row.family_id, expires_at=row.expires_at)

    try:
        await redis_service.cache_refresh_rotation(
            old_token_hash,
            json.dumps(
                {
                    "raw_token": issued.raw_token,
                    "user_id": str(issued.user_id),
                    "family_id": str(issued.family_id),
                    "expires_at": issued.expires_at.isoformat(),
                }
            ),
            ttl=settings.REFRESH_TOKEN_GRACE_PERIOD_SECONDS,
        )
    except RedisError:
        # Best-effort: if this fails, a genuine race in this exact window
        # gets misread as reuse and the family is revoked — annoying, but
        # never a security hole (the client just has to log in again).
        logger.warning("Redis unavailable — could not cache grace-period reissue for family %s.", issued.family_id)

    return issued

# app/api/v1/endpoints/auth.py
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limiter import (
    enforce_login_rate_limit,
    enforce_registration_rate_limit,
    enforce_password_reset_rate_limit,
)
from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    create_password_reset_token,
    decode_password_reset_token,
    verify_google_id_token,
    verify_google_access_token,
)
from app.db.models.user import User
from app.services import email_service
from app.services.redis_service import redis_service
from app.schemas.auth import (
    RegisterRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserResponse,
    AuthResponse,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    GoogleAuthRequest,
    GoogleConfigResponse,
    MessageResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def _enforce_account_lockout(email: str) -> None:
    # Per-IP limiting (the dependency on this route) is blind to the same
    # account being targeted from many different IPs — a botnet or proxy
    # rotation spreads attempts thin enough that no single IP ever crosses
    # the per-IP threshold. This is a second, independent counter keyed by
    # the account itself, so the account is still protected either way.
    try:
        attempts = await redis_service.increment_counter(
            f"account-lockout:{email}",
            ttl=settings.ACCOUNT_LOGIN_LOCKOUT_WINDOW_SECONDS,
        )
    except RedisError:
        logger.warning("Redis unavailable — skipping account-level lockout check for %s.", email)
        return

    if attempts > settings.ACCOUNT_LOGIN_LOCKOUT_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "account_locked",
                "message": "Too many failed attempts for this account. Please try again later.",
            },
        )


def _user_to_response(user: User) -> UserResponse:
    initials = user.initials or "".join(part[0].upper() for part in (user.full_name or "").split()[:2])
    return UserResponse(
        id=str(user.id),
        name=user.full_name,
        email=user.email,
        role=user.role,
        plan=user.plan,
        initials=initials or None,
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_registration_rate_limit)],
)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "email_taken", "message": "An account with this email already exists."},
        )

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Two concurrent registrations for the same email can both pass the
        # SELECT check above and race to commit — the DB's unique constraint
        # is the real guard; this turns that race into a clean 400 instead
        # of an unhandled 500.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "email_taken", "message": "An account with this email already exists."},
        )
    await db.refresh(user)

    token = TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=create_refresh_token(str(user.id)),
    )
    return AuthResponse(token=token, user=_user_to_response(user))


@router.post("/login", response_model=AuthResponse, dependencies=[Depends(enforce_login_rate_limit)])
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    await _enforce_account_lockout(payload.email)

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalars().first()

    # A Google-only account has no hashed_password (None) — verify_password
    # would crash on that instead of failing cleanly, so it must short
    # circuit here rather than fall through to the hash comparison.
    if not user or not user.hashed_password or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials", "message": "Incorrect email or password."},
        )

    # A successful login clears this IP's and this account's failed-attempt
    # counters, so past failures (e.g. from a shared/office IP, or a
    # mistyped password a minute ago) don't count against the next
    # legitimate attempt. If Redis is down this is a no-op — the login
    # itself already succeeded and must not fail over a housekeeping step.
    try:
        await redis_service.reset_counter(request.state.rate_limit_key)
        await redis_service.reset_counter(f"account-lockout:{payload.email}")
    except RedisError:
        logger.warning("Redis unavailable — could not reset login rate-limit counters for user %s.", user.id)

    token = TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=create_refresh_token(str(user.id)),
    )
    return AuthResponse(token=token, user=_user_to_response(user))


@router.get("/google/config", response_model=GoogleConfigResponse)
async def google_config():
    """
    Public, unauthenticated: lets the Flutter app initialize Google
    Sign-In with the same Web Client ID this server verifies tokens
    against, rather than keeping its own separate hardcoded copy that can
    drift out of sync with this one.
    """
    return GoogleConfigResponse(
        web_client_id=settings.GOOGLE_CLIENT_ID,
        configured=bool(settings.GOOGLE_CLIENT_ID),
    )


@router.post(
    "/google",
    response_model=AuthResponse,
    dependencies=[Depends(enforce_login_rate_limit)],
)
async def google_auth(payload: GoogleAuthRequest, db: AsyncSession = Depends(get_db)):
    """
    Verify a Google credential from the Flutter app — either an OIDC
    id_token or an OAuth access_token, whichever that platform's Google
    Sign-In SDK produced — then sign the caller in. Creates a new account
    on first sign-in, or links google_id onto an existing password
    account that shares the same Google-verified email.
    """
    idinfo = None

    if payload.id_token:
        if not settings.GOOGLE_CLIENT_ID:
            # Without this, an empty GOOGLE_CLIENT_ID means
            # verify_oauth2_token checks the token's "aud" claim against
            # "" — which never matches a real token — so every attempt
            # would fail with the same confusing "invalid token" error as
            # an actually-forged one, with nothing pointing at the real
            # cause (server misconfiguration, not a bad token). This only
            # applies to the id_token path — the access_token path below
            # asks Google directly and never checks GOOGLE_CLIENT_ID.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "google_oauth_not_configured",
                    "message": "Google Sign-In is not configured on the server yet. Please sign in with email and password.",
                },
            )
        try:
            # verify_oauth2_token fetches Google's certs over the network
            # — off the event loop for the same reason as the Celery
            # .delay() calls elsewhere in this codebase: a slow/
            # unreachable Google endpoint must not stall this coroutine
            # indefinitely.
            idinfo = await asyncio.to_thread(verify_google_id_token, payload.id_token)
        except HTTPException:
            idinfo = None
    elif payload.access_token:
        try:
            idinfo = await verify_google_access_token(payload.access_token)
        except HTTPException:
            idinfo = None

    if idinfo is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_google_token", "message": "Invalid or missing Google authentication token"},
        )

    if not idinfo.get("email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "email_not_verified", "message": "Google account email is not verified."},
        )

    google_id = idinfo["sub"]
    # Preserved exactly as Google returns it — same case-preserving policy
    # as password-based register/login, so this doesn't create a
    # "third casing" of the same address that neither of those paths
    # would ever produce or match against.
    email = idinfo["email"].strip()
    name = idinfo.get("name")

    result = await db.execute(select(User).where(User.google_id == google_id))
    user = result.scalars().first()

    if not user:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()

        if user:
            # Pre-existing password account with this exact email. Google
            # has already proven the caller owns that email (that's what
            # email_verified means), so linking here is the same trust
            # level a brand-new signup gets — not a weaker one.
            user.google_id = google_id
        else:
            user = User(
                email=email,
                hashed_password=None,
                full_name=name,
                google_id=google_id,
            )
            db.add(user)

        try:
            await db.commit()
        except IntegrityError:
            # Two concurrent first-time Google sign-ins for the same
            # account can both pass the SELECT checks above and race to
            # commit — same shape as the /register race.
            await db.rollback()
            result = await db.execute(select(User).where(User.google_id == google_id))
            user = result.scalars().first()
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "conflict", "message": "Please try signing in again."},
                )
        else:
            await db.refresh(user)

    token = TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=create_refresh_token(str(user.id)),
    )
    return AuthResponse(token=token, user=_user_to_response(user))


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    user_id, jti = decode_refresh_token(payload.refresh_token)

    # Revocation is a defense-in-depth check on top of an already
    # signature-verified, non-expired token. If Redis is unreachable we
    # cannot confirm revocation either way — fail open (treat as not
    # revoked) rather than blocking every token refresh in the app during
    # a Redis outage that has nothing to do with this user's credentials.
    try:
        revoked = await redis_service.is_refresh_token_revoked(jti)
    except RedisError:
        logger.warning("Redis unavailable — skipping refresh-token revocation check for user %s.", user_id)
        revoked = False

    if revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "token_revoked", "message": "This session has been logged out."},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    if not result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_not_found", "message": "User no longer exists."},
        )

    return TokenResponse(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(payload: RefreshRequest):
    # Revoking is best-effort keyed off the refresh token's own jti — an
    # already-invalid/expired token has nothing to revoke, so treat that
    # as a no-op rather than an error; the caller's intent (end this
    # session) is already satisfied either way.
    try:
        _, jti = decode_refresh_token(payload.refresh_token)
    except HTTPException:
        return MessageResponse(message="Logged out.")

    try:
        await redis_service.revoke_refresh_token(jti, ttl=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400)
    except RedisError:
        logger.warning("Redis unavailable — could not persist refresh-token revocation for jti %s.", jti)

    return MessageResponse(message="Logged out.")


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    dependencies=[Depends(enforce_password_reset_rate_limit)],
)
async def forgot_password(payload: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """
    Generate a password reset token and email it — or, if SMTP isn't
    configured (SMTP_HOST/USER/PASSWORD/FROM_EMAIL in .env), fall back to
    printing the link to the console for local development.

    Always returns 200 regardless of whether the email exists or whether
    delivery succeeded — this prevents user enumeration attacks.
    """
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalars().first()

    if user:
        reset_token = create_password_reset_token(str(user.id))
        reset_link = f"https://yourapp.com/reset-password?token={reset_token}"
        sent = await email_service.send_password_reset_email(payload.email, reset_link)
        if not sent:
            print(f"\n[DEV] Password reset link for {payload.email}:\n  {reset_link}\n")

    return MessageResponse(
        message="If that email is registered you will receive a reset link shortly."
    )


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    dependencies=[Depends(enforce_password_reset_rate_limit)],
)
async def reset_password(payload: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """
    Validate the password-reset JWT and update the user's password.
    """
    user_id, jti = decode_password_reset_token(payload.token)

    # Reject replays of an already-consumed reset link before it naturally
    # expires — otherwise a link leaked once (email logs, browser history)
    # stays exploitable for its whole validity window. Same fail-open
    # policy as the other Redis-backed checks in this file: if Redis is
    # down we can't confirm single-use either way, and blocking every
    # password reset in the app over that is worse than the rare risk of
    # a replay during an outage window.
    try:
        already_used = await redis_service.is_password_reset_token_used(jti)
    except RedisError:
        logger.warning("Redis unavailable — skipping used-token check for password reset (user %s).", user_id)
        already_used = False

    if already_used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "token_already_used", "message": "This reset link has already been used."},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "User not found."},
        )

    if verify_password(payload.new_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "same_password",
                "message": "New password must be different from your current password.",
            },
        )

    user.hashed_password = hash_password(payload.new_password)
    await db.commit()

    try:
        await redis_service.mark_password_reset_token_used(
            jti, ttl=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES * 60
        )
    except RedisError:
        logger.warning("Redis unavailable — could not mark password-reset token used (user %s).", user_id)

    return MessageResponse(message="Password has been reset successfully.")
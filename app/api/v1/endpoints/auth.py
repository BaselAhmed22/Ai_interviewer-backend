# app/api/v1/endpoints/auth.py
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
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
    get_client_ip,
)
from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_password_reset_token,
    decode_password_reset_token,
    verify_google_id_token,
    verify_google_access_token,
    get_current_user_id,
)
from app.db.models.user import User, is_profile_complete
from app.services import email_service, token_service
from app.services.redis_service import redis_service
from app.services.token_service import IssuedToken
from app.schemas.auth import (
    RegisterRequest,
    LoginRequest,
    RefreshRequest,
    CompleteProfileRequest,
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

_REFRESH_COOKIE_PATH = f"{settings.API_V1_STR}/auth"


def _set_refresh_cookie(response: Response, issued: IssuedToken) -> None:
    max_age = max(0, int((issued.expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(
        key=settings.REFRESH_TOKEN_COOKIE_NAME,
        value=issued.raw_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="strict",
        path=_REFRESH_COOKIE_PATH,
        max_age=max_age,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.REFRESH_TOKEN_COOKIE_NAME,
        path=_REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="strict",
    )


def _extract_refresh_token(request: Request, payload: Optional[RefreshRequest]) -> str:
    # Cookie takes precedence over the body for web clients.
    cookie_value = request.cookies.get(settings.REFRESH_TOKEN_COOKIE_NAME)
    if cookie_value:
        return cookie_value
    if payload and payload.refresh_token:
        return payload.refresh_token
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "missing_refresh_token", "message": "No refresh token provided."},
    )


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
        first_name=user.first_name,
        last_name=user.last_name,
        phone_country_code=user.phone_country_code,
        phone_number=user.phone_number,
        university=user.university,
        faculty=user.faculty,
        is_graduate=user.is_graduate,
        graduation_year=user.graduation_year,
        is_profile_complete=is_profile_complete(user),
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_registration_rate_limit)],
)
async def register(payload: RegisterRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
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
        first_name=payload.first_name,
        last_name=payload.last_name,
        phone_country_code=payload.phone_country_code.value if payload.phone_country_code else None,
        phone_number=payload.phone_number,
        university=payload.university,
        faculty=payload.faculty,
        is_graduate=payload.is_graduate,
        graduation_year=payload.graduation_year,
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

    issued = await token_service.issue_new_family(
        db, user.id, get_client_ip(request), request.headers.get("user-agent")
    )
    _set_refresh_cookie(response, issued)

    token = TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=issued.raw_token,
    )
    return AuthResponse(token=token, user=_user_to_response(user))


@router.post("/login", response_model=AuthResponse, dependencies=[Depends(enforce_login_rate_limit)])
async def login(
    payload: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
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

    issued = await token_service.issue_new_family(
        db, user.id, get_client_ip(request), request.headers.get("user-agent")
    )
    _set_refresh_cookie(response, issued)

    token = TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=issued.raw_token,
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
async def google_auth(
    payload: GoogleAuthRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
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
        except HTTPException as exc:
            # 503 (Google itself unreachable) is its own distinct, already-
            # correct response — only a genuinely invalid/expired token
            # should fall through to the generic 400 below.
            if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
                raise
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

    issued = await token_service.issue_new_family(
        db, user.id, get_client_ip(request), request.headers.get("user-agent")
    )
    _set_refresh_cookie(response, issued)

    token = TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=issued.raw_token,
    )
    return AuthResponse(token=token, user=_user_to_response(user))


@router.get("/me", response_model=UserResponse)
async def get_me(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    """Current user's profile, including isProfileComplete — lets the app
    re-check completion status on every cold start without another login."""
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "user_not_found", "message": "User not found."})
    return _user_to_response(user)


@router.patch("/profile", response_model=UserResponse)
async def complete_profile(
    payload: CompleteProfileRequest, user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    """Fills in the mandatory profile fields (first/last name, education,
    phone) a Google sign-in doesn't collect on its own — required before
    starting an interview session (see session_service.ensure_ready_to_start)."""
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "user_not_found", "message": "User not found."})

    user.first_name = payload.first_name
    user.last_name = payload.last_name
    user.phone_country_code = payload.phone_country_code.value
    user.phone_number = payload.phone_number
    user.university = payload.university
    user.faculty = payload.faculty
    user.is_graduate = payload.is_graduate
    user.graduation_year = payload.graduation_year if payload.is_graduate else None
    await db.commit()
    await db.refresh(user)

    return _user_to_response(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    payload: Optional[RefreshRequest] = None,
    db: AsyncSession = Depends(get_db),
):
    """Rotates the refresh token (see app.services.token_service)."""
    raw_token = _extract_refresh_token(request, payload)

    issued = await token_service.rotate_refresh_token(
        db, raw_token, get_client_ip(request), request.headers.get("user-agent")
    )

    result = await db.execute(select(User).where(User.id == issued.user_id))
    if not result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_not_found", "message": "User no longer exists."},
        )

    _set_refresh_cookie(response, issued)

    return TokenResponse(
        access_token=create_access_token(str(issued.user_id)),
        refresh_token=issued.raw_token,
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    payload: Optional[RefreshRequest] = None,
    db: AsyncSession = Depends(get_db),
):
    """Revokes every token in the family and clears the refresh cookie."""
    cookie_value = request.cookies.get(settings.REFRESH_TOKEN_COOKIE_NAME)
    raw_token = cookie_value or (payload.refresh_token if payload else None)

    if raw_token:
        await token_service.revoke_family_by_token(db, raw_token)

    _clear_refresh_cookie(response)
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
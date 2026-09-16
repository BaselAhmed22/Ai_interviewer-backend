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
from app.auth.models import AccountType as DBAccountType, User, is_profile_complete
from app.auth.services import email_service, token_service
from app.core.redis_service import redis_service
from app.auth.services.token_service import IssuedToken
from app.auth.schemas import (
    AccountType as SchemaAccountType,
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
    # A second counter keyed by the account itself, independent of the
    # per-IP limit on this route — catches a botnet/proxy rotation
    # spreading attempts thin enough to stay under the per-IP threshold.
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
        is_approved=user.is_approved,
        interview_attempts_remaining=max(0, settings.FREE_INTERVIEW_ATTEMPTS - user.interview_attempts_used),
        account_type=user.account_type.value,
        company_name=user.company_name,
        country=user.country,
        position=user.position,
        specialization=user.specialization,
        company_size=user.company_size,
        website=user.website,
        is_company_admin=user.is_company_admin,
    )


def _resolve_new_user_role(email: str) -> tuple[Optional[str], bool]:
    """Bootstrap admins listed in ADMIN_EMAILS register with role="admin".
    All users default to is_approved=True so no approval gate blocks login/interviews."""
    admin_emails = {e.strip().lower() for e in settings.ADMIN_EMAILS.split(",") if e.strip()}
    if email.lower() in admin_emails:
        return "admin", True
    return None, True


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

    role, is_approved = _resolve_new_user_role(payload.email)
    is_company = payload.account_type == SchemaAccountType.COMPANY
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
        role=role,
        is_approved=is_approved,
        account_type=DBAccountType(payload.account_type.value),
        # RegisterRequest's own validator (_CompanyFieldsMixin) already
        # guarantees these are populated for a company account and None
        # otherwise — no need to re-branch on is_company here.
        company_name=payload.company_name,
        country=payload.country,
        position=payload.position,
        specialization=payload.specialization,
        company_size=payload.company_size.value if payload.company_size else None,
        website=payload.website,
        # The registering user is automatically the company's owner/admin
        # — there's no separate "invite the first admin" step.
        is_company_admin=is_company,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Two concurrent registrations can both pass the SELECT above and
        # race to commit — the DB's unique constraint is the real guard.
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

    # A Google-only account has no hashed_password — verify_password would
    # crash on None, so short-circuit before the hash comparison.
    if not user or not user.hashed_password or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials", "message": "Incorrect email or password."},
        )

    # Clear both counters so past failures don't count against future
    # attempts. Redis being down here is a no-op, not a login failure.
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
            # An empty GOOGLE_CLIENT_ID would make verify_oauth2_token check
            # "aud" against "", failing every real token with the same
            # error as a forged one — fail loudly here instead. Only
            # applies to id_token; access_token below asks Google directly.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "google_oauth_not_configured",
                    "message": "Google Sign-In is not configured on the server yet. Please sign in with email and password.",
                },
            )
        try:
            # verify_oauth2_token fetches Google's certs over the network —
            # off the event loop so a slow/unreachable Google can't stall it.
            idinfo = await asyncio.to_thread(verify_google_id_token, payload.id_token)
        except HTTPException as exc:
            # 503 (Google unreachable) is already the right response —
            # only a genuinely invalid token should fall through to 400.
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
    # Case preserved as-is, matching password register/login's policy.
    email = idinfo["email"].strip()
    name = idinfo.get("name")

    result = await db.execute(select(User).where(User.google_id == google_id))
    user = result.scalars().first()

    if not user:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()

        if user:
            # Existing password account, same verified email — link it.
            user.google_id = google_id
        else:
            role, is_approved = _resolve_new_user_role(email)
            user = User(
                email=email,
                hashed_password=None,
                full_name=name,
                google_id=google_id,
                role=role,
                is_approved=is_approved,
            )
            db.add(user)

        try:
            await db.commit()
        except IntegrityError:
            # Same race as /register: two concurrent first-time sign-ins.
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
    """Rotates the refresh token (see app.auth.services.token_service)."""
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
    """Generate a password reset token and email it, or print the link to
    the console if SMTP isn't configured. Always returns 200 regardless of
    whether the email exists, to prevent user enumeration."""
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
    """Validate the password-reset JWT and update the user's password."""
    user_id, jti = decode_password_reset_token(payload.token)

    # Reject replays of an already-used reset link. Fail-open if Redis is
    # down, same as the other Redis-backed checks here — blocking every
    # reset over that is worse than the rare replay risk during an outage.
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
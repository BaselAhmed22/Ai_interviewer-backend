import enum
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, Boolean, Enum, Integer, Index, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class AccountType(str, enum.Enum):
    USER = "user"
    COMPANY = "company"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # Null for a Google-only account until it sets a password via "forgot password".
    hashed_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # E.g. "+20" / "+966" — see app.auth.schemas._COUNTRY_PHONE_LENGTHS for
    # the supported codes and the exact digit count each one requires.
    phone_country_code: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    # National number only, digits with no country code/spaces/symbols.
    phone_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    university: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    faculty: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # True = graduate, False = undergraduate, NULL = not provided.
    is_graduate: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Only meaningful when is_graduate is True; left null for undergraduates.
    graduation_year: Mapped[Optional[int]] = mapped_column(nullable=True)

    role: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Google's stable "sub" claim — null for password-only accounts.
    google_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    plan: Mapped[str] = mapped_column(String(50), default="Free Plan")
    initials: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)

    # Individual (candidate practicing interviews) vs company (recruiter/
    # org registering to eventually manage a team) — set once at
    # registration, see app.auth.schemas.RegisterRequest. Google sign-in
    # always creates a USER account; there's no company-signup path via
    # Google since that flow collects no extra fields.
    account_type: Mapped[AccountType] = mapped_column(
        # values_callable: without it, SQLAlchemy stores the enum
        # member's NAME ("USER") against the native Postgres enum, which
        # only has the lowercase VALUEs ("user"/"company") the migration
        # created — every insert would fail with an invalid-enum-value error.
        Enum(AccountType, name="accounttype", values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        default=AccountType.USER,
        nullable=False,
    )
    # The remaining fields are set only for account_type=COMPANY (enforced
    # by RegisterRequest, not at the DB level) — this is this user's own
    # employer, unrelated to app.candidates.models.InterviewPreference
    # .company_name (the target company a candidate is interviewing at).
    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    position: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    specialization: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Free-text bucket (e.g. "1-10", "11-50", "50+") — see
    # app.auth.schemas.CompanySize. Optional even for company accounts.
    company_size: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # True only for the user who registered the company account — they're
    # its owner/admin. No team-invite flow exists yet; this flag is the
    # foundation for one.
    is_company_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Every account defaults to approved — no manual admin approval gate
    # required before logging in or starting interviews.
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Successful POST /interviews/prepare calls, capped at
    # settings.FREE_INTERVIEW_ATTEMPTS. Admins are exempt from the cap but
    # this still counts for them, informationally.
    interview_attempts_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def is_admin(user: "User") -> bool:
    return user.role == "admin"


# Mandatory profile fields — checked regardless of how the account was
# created (password or Google), so a Google sign-in can't bypass the
# same requirement a password registration is also held to.
_REQUIRED_PROFILE_FIELDS = (
    "first_name", "last_name", "is_graduate", "university", "faculty",
    "phone_country_code", "phone_number",
)


def is_profile_complete(user: "User") -> bool:
    # This gate (university/faculty/graduation year) is candidate-shaped
    # and doesn't apply to a company account — it isn't practicing
    # interviews itself, so it's exempt rather than permanently unable to
    # satisfy fields it was never asked to fill in.
    if user.account_type == AccountType.COMPANY:
        return True
    if any(getattr(user, field) is None for field in _REQUIRED_PROFILE_FIELDS):
        return False
    if user.is_graduate and user.graduation_year is None:
        return False
    return True


# Opaque, rotating refresh tokens grouped into families. See
# app/auth/services/token_service.py for the rotation and reuse-detection logic.


class RefreshTokenStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_family_status", "family_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    # Shared by every token descended from one login — the unit of
    # revocation for both logout and reuse-attack response.
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)

    # Denormalized onto every row so the absolute expiry check needs no
    # join back to the family's first token.
    family_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[RefreshTokenStatus] = mapped_column(
        Enum(RefreshTokenStatus, name="refreshtokenstatus"), default=RefreshTokenStatus.ACTIVE, nullable=False
    )

    # Audit trail for the rotation chain — which token replaced this one.
    replaced_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )

    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Fixed at family creation and never extended by rotation — this is
    # the absolute session cap (REFRESH_TOKEN_ABSOLUTE_EXPIRE_DAYS).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

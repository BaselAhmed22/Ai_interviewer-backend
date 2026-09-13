# User Database Model
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Text, DateTime, Boolean, Index, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # Nullable because a Google-only account never sets a password — it
    # authenticates solely via a verified Google ID token. If that user
    # later uses "forgot password" this gets populated and both login
    # paths work from then on.
    hashed_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # E.g. "+20" / "+966" — see app.schemas.auth._COUNTRY_PHONE_LENGTHS for
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
    # Google's stable "sub" claim for this user. Unique + nullable: most
    # rows (password-only accounts) leave it null; a Google-authenticated
    # account gets it set once, either on first Google sign-in (new user)
    # or linked onto a pre-existing password account that shares the same
    # Google-verified email.
    google_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    plan: Mapped[str] = mapped_column(String(50), default="Free Plan")
    initials: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Mandatory profile fields — checked regardless of how the account was
# created (password or Google), so a Google sign-in can't bypass the
# same requirement a password registration is also held to.
_REQUIRED_PROFILE_FIELDS = (
    "first_name", "last_name", "is_graduate", "university", "faculty",
    "phone_country_code", "phone_number",
)


def is_profile_complete(user: "User") -> bool:
    if any(getattr(user, field) is None for field in _REQUIRED_PROFILE_FIELDS):
        return False
    if user.is_graduate and user.graduation_year is None:
        return False
    return True


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"
    __table_args__ = (
        Index(
            "ux_candidate_profiles_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    raw_cv_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cv_file_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # The name the candidate uploaded the file as (e.g. "Ahmed_CV.pdf") —
    # separate from cv_file_path, which is the randomized on-disk name
    # used to avoid collisions. Without this, downloads had no way to
    # hand the candidate back a recognizable filename.
    original_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    skills: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    processing_failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobDescription(Base):
    __tablename__ = "job_descriptions"
    __table_args__ = (
        Index(
            "ux_job_descriptions_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    job_title: Mapped[str] = mapped_column(String(255), nullable=False)
    description_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InterviewPreference(Base):
    __tablename__ = "interview_preferences"
    __table_args__ = (
        Index(
            "ux_interview_preferences_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False) 
    job_title: Mapped[str] = mapped_column(String(255), nullable=False)    
    language: Mapped[str] = mapped_column(String(20), default="en")        
    interview_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
import re
from typing import Optional
from email_validator import validate_email, EmailNotValidError
from pydantic import Field, field_validator
from app.schemas.base import CamelModel, NoControlCharsMixin


class _ExactEmailMixin:
    # Case-preserving by design: pydantic's own EmailStr silently
    # lowercases the domain part as part of its normalization (verified:
    # "Test@Example.COM" -> "Test@example.com") — using it here would
    # defeat the point, so the field is a plain `str` and this validator
    # does its own format check via email_validator directly, then
    # discards the library's normalized result and returns the original
    # (only whitespace-trimmed) string. Postgres `=` on varchar is already
    # exact/byte-for-byte, so once nothing here touches case, "User@x.com"
    # and "user@x.com" are simply two distinct, independently valid
    # addresses all the way through register/login/forgot-password.
    @field_validator("email", mode="before")
    @classmethod
    def _validate_format_preserve_case(cls, value):
        if not isinstance(value, str):
            raise ValueError("Email must be a string.")
        raw = value.strip()
        try:
            validate_email(raw, check_deliverability=False)
        except EmailNotValidError as exc:
            raise ValueError(str(exc)) from exc
        return raw


_PASSWORD_UPPER = re.compile(r"[A-Z]")
_PASSWORD_LOWER = re.compile(r"[a-z]")
_PASSWORD_DIGIT = re.compile(r"\d")
_PASSWORD_SPECIAL = re.compile(r"[^A-Za-z0-9]")


class _StrongPasswordMixin:
    # Length alone (min_length=8 on the field) doesn't stop "aaaaaaaa" —
    # this enforces the actual complexity bar so a 422 with a specific
    # reason comes back at request time instead of the account ending up
    # protected by a password that's cheap to brute-force.
    @field_validator("password", "new_password", check_fields=False)
    @classmethod
    def _validate_password_strength(cls, value: str) -> str:
        if not _PASSWORD_UPPER.search(value):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not _PASSWORD_LOWER.search(value):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not _PASSWORD_DIGIT.search(value):
            raise ValueError("Password must contain at least one digit.")
        if not _PASSWORD_SPECIAL.search(value):
            raise ValueError("Password must contain at least one special character.")
        return value


class RegisterRequest(_ExactEmailMixin, _StrongPasswordMixin, NoControlCharsMixin, CamelModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=255)

class LoginRequest(_ExactEmailMixin, CamelModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1)

class RefreshRequest(CamelModel):
    refresh_token: str = Field(min_length=1)

class TokenResponse(CamelModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class UserResponse(CamelModel):
    id: str
    name: Optional[str] = None
    email: str
    role: Optional[str] = None
    plan: str
    initials: Optional[str] = None

class AuthResponse(CamelModel):
    token: TokenResponse
    user: UserResponse


class ForgotPasswordRequest(_ExactEmailMixin, CamelModel):
    # Case-sensitive lookup, same as login: since two accounts can now
    # coexist differing only by case, an exact match is the only way to
    # target the right one instead of guessing/normalizing to whichever
    # variant happens to exist.
    email: str = Field(min_length=3, max_length=254)


class ResetPasswordRequest(_StrongPasswordMixin, CamelModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


class MessageResponse(CamelModel):
    message: str


class GoogleAuthRequest(CamelModel):
    # Different Google Sign-In client libraries hand back different token
    # types depending on platform (web vs. iOS vs. Android SDK version) —
    # accepting either here means the Flutter app doesn't need
    # platform-specific backend calls. Both optional at the schema level
    # on purpose: "neither provided" is a valid, well-formed request that
    # the endpoint itself rejects with a specific 400, not a generic 422
    # from a cross-field validator.
    id_token: Optional[str] = None
    access_token: Optional[str] = None


class GoogleConfigResponse(CamelModel):
    # Lets the Flutter app fetch the exact same Web Client ID this
    # backend verifies incoming id_tokens against, instead of keeping a
    # separate hardcoded copy that can silently drift out of sync (the
    # app requests a token for client X, this server checks the token's
    # "aud" against client Y — verification then fails for every user
    # with no obvious cause). `configured=False` is the server-side
    # source of truth behind the "not configured" message the app shows.
    web_client_id: str
    configured: bool
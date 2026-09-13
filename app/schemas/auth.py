import enum
import re
from typing import Optional
from email_validator import validate_email, EmailNotValidError
from pydantic import Field, field_validator, model_validator
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

class CountryCode(str, enum.Enum):
    EGYPT = "+20"
    SAUDI_ARABIA = "+966"
    UAE = "+971"
    KUWAIT = "+965"
    QATAR = "+974"
    BAHRAIN = "+973"
    OMAN = "+968"
    JORDAN = "+962"
    US_CANADA = "+1"
    UK = "+44"


# National significant number length (digits only, country code excluded)
# expected for each supported country code. Add a country by adding one
# member to CountryCode above and one line here.
_COUNTRY_PHONE_LENGTHS: dict[CountryCode, int] = {
    CountryCode.EGYPT: 10,
    CountryCode.SAUDI_ARABIA: 9,
    CountryCode.UAE: 9,
    CountryCode.KUWAIT: 8,
    CountryCode.QATAR: 8,
    CountryCode.BAHRAIN: 8,
    CountryCode.OMAN: 8,
    CountryCode.JORDAN: 9,
    CountryCode.US_CANADA: 10,
    CountryCode.UK: 10,
}


class _PhoneNumberMixin:
    @model_validator(mode="after")
    def _validate_phone_number(self):
        if self.phone_country_code is None and self.phone_number is None:
            return self
        if self.phone_country_code is None or self.phone_number is None:
            raise ValueError("phone_country_code and phone_number must be provided together.")

        if not self.phone_number.isdigit():
            raise ValueError("Phone number must contain digits only, without the country code.")

        expected_length = _COUNTRY_PHONE_LENGTHS[self.phone_country_code]
        if len(self.phone_number) != expected_length:
            raise ValueError(
                f"Phone number for {self.phone_country_code.value} must be exactly {expected_length} digits."
            )
        return self


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


class RegisterRequest(_ExactEmailMixin, _StrongPasswordMixin, _PhoneNumberMixin, NoControlCharsMixin, CamelModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=255)

    first_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    phone_country_code: Optional[CountryCode] = None
    phone_number: Optional[str] = Field(default=None, min_length=4, max_length=15)
    university: Optional[str] = Field(default=None, min_length=1, max_length=255)
    faculty: Optional[str] = Field(default=None, min_length=1, max_length=255)
    is_graduate: Optional[bool] = None
    # Only meaningful when is_graduate is true; left blank for undergraduates.
    graduation_year: Optional[int] = Field(default=None, ge=1950, le=2100)

class CompleteProfileRequest(_PhoneNumberMixin, NoControlCharsMixin, CamelModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone_country_code: CountryCode
    phone_number: str = Field(min_length=4, max_length=15)
    university: str = Field(min_length=1, max_length=255)
    faculty: str = Field(min_length=1, max_length=255)
    is_graduate: bool
    graduation_year: Optional[int] = Field(default=None, ge=1950, le=2100)

    @model_validator(mode="after")
    def _validate_graduation_year(self):
        if self.is_graduate and self.graduation_year is None:
            raise ValueError("graduation_year is required for graduates.")
        return self


class LoginRequest(_ExactEmailMixin, CamelModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1)

class RefreshRequest(CamelModel):
    # Optional: a browser client sends the refresh token only via the
    # HttpOnly cookie and posts no body at all; a mobile client (which
    # can't rely on HttpOnly cookies the same way) sends it here instead.
    # The endpoint checks the cookie first, then falls back to this field.
    refresh_token: Optional[str] = Field(default=None, min_length=1)

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

    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone_country_code: Optional[CountryCode] = None
    phone_number: Optional[str] = None
    university: Optional[str] = None
    faculty: Optional[str] = None
    is_graduate: Optional[bool] = None
    graduation_year: Optional[int] = None
    is_profile_complete: bool = False

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
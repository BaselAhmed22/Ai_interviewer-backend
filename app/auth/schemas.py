import enum
import re
from typing import Optional
from email_validator import validate_email, EmailNotValidError
from pydantic import Field, field_validator, model_validator
from app.core.schemas_base import CamelModel, NoControlCharsMixin


class _ExactEmailMixin:
    # Case-preserving by design: pydantic's EmailStr lowercases the domain
    # as part of normalization, so the field is a plain str and this
    # validates format via email_validator without adopting its
    # normalized result — "User@x.com" and "user@x.com" stay distinct.
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


class AccountType(str, enum.Enum):
    USER = "user"
    COMPANY = "company"


class CompanySize(str, enum.Enum):
    SIZE_1_10 = "1-10"
    SIZE_11_50 = "11-50"
    SIZE_50_PLUS = "50+"


class _CompanyFieldsMixin:
    """Enforces the dual-registration split: company_name/country/position/
    specialization are required when account_type is COMPANY, and cleared
    — regardless of what was submitted — when it's USER, so an individual
    account can never end up carrying stray company data."""

    @model_validator(mode="after")
    def _validate_company_fields(self):
        required = ("company_name", "country", "position", "specialization")
        if self.account_type == AccountType.COMPANY:
            missing = [name for name in required if not getattr(self, name)]
            if missing:
                raise ValueError(f"Company accounts require: {', '.join(missing)}.")
        else:
            for name in (*required, "company_size", "website"):
                setattr(self, name, None)
        return self


class _StrongPasswordMixin:
    # min_length=8 alone doesn't stop "aaaaaaaa" — this enforces the
    # actual complexity bar.
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


class RegisterRequest(
    _ExactEmailMixin, _StrongPasswordMixin, _PhoneNumberMixin, _CompanyFieldsMixin, NoControlCharsMixin, CamelModel
):
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

    # Individual vs company — see _CompanyFieldsMixin for the cross-field
    # rules the four fields below and company_size/website are held to.
    account_type: AccountType = AccountType.USER
    company_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    country: Optional[str] = Field(default=None, min_length=1, max_length=100)
    position: Optional[str] = Field(default=None, min_length=1, max_length=150)
    specialization: Optional[str] = Field(default=None, min_length=1, max_length=255)
    company_size: Optional[CompanySize] = None
    website: Optional[str] = Field(default=None, max_length=255)

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
    is_approved: bool = False
    interview_attempts_remaining: int = 0

    account_type: AccountType = AccountType.USER
    company_name: Optional[str] = None
    country: Optional[str] = None
    position: Optional[str] = None
    specialization: Optional[str] = None
    company_size: Optional[CompanySize] = None
    website: Optional[str] = None
    is_company_admin: bool = False

class AuthResponse(CamelModel):
    token: TokenResponse
    user: UserResponse


class ForgotPasswordRequest(_ExactEmailMixin, CamelModel):
    # Case-sensitive lookup, same as login — two accounts can coexist
    # differing only by case.
    email: str = Field(min_length=3, max_length=254)


class ResetPasswordRequest(_StrongPasswordMixin, CamelModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


class MessageResponse(CamelModel):
    message: str


class GoogleAuthRequest(CamelModel):
    # Different platforms' Google Sign-In SDKs return different token
    # types — accepting either avoids platform-specific backend calls.
    # Both optional: "neither provided" gets a specific 400 from the
    # endpoint, not a generic 422 from a cross-field validator.
    id_token: Optional[str] = None
    access_token: Optional[str] = None


class GoogleConfigResponse(CamelModel):
    # Lets the app fetch the same Web Client ID this backend verifies
    # id_tokens against, instead of a hardcoded copy that can drift out of sync.
    web_client_id: str
    configured: bool
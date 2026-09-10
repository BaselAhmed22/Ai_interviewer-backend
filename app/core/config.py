from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> project root. Anchoring every
# on-disk path (uploads, etc.) to this absolute location is what makes a
# path stored by FastAPI resolvable by the Celery worker: they run as
# separate OS processes and each has its own current working directory,
# so a relative path ("uploads/cvs/x.pdf") silently resolves against
# whichever directory each process happened to be launched from.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    PROJECT_NAME: str = "IntervYou AI Backend"
    API_V1_STR: str = "/api/v1"
    DEBUG: bool = False

    # No default on purpose: a hardcoded fallback secret here would let
    # anyone forge valid JWTs for any user if SECRET_KEY is ever left
    # unset in an environment. Missing it must be a hard startup failure.
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30

    # Checked on every decode alongside the signature — a token signed
    # with the right SECRET_KEY but minted for a different audience (a
    # future second service sharing this key, say) is rejected instead of
    # being silently accepted just because the signature checks out.
    JWT_ISSUER: str = "intervyou-backend"
    JWT_AUDIENCE: str = "intervyou-app"

    LOGIN_RATE_LIMIT_MAX_ATTEMPTS: int = 5
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 60
    REGISTER_RATE_LIMIT_MAX_ATTEMPTS: int = 5
    REGISTER_RATE_LIMIT_WINDOW_SECONDS: int = 300
    PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS: int = 3
    PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS: int = 300

    # Per-IP limiting (above) is blind to the same account being targeted
    # from many different IPs (botnet / proxy rotation). This is a second,
    # independent counter keyed by the email itself.
    ACCOUNT_LOGIN_LOCKOUT_MAX_ATTEMPTS: int = 10
    ACCOUNT_LOGIN_LOCKOUT_WINDOW_SECONDS: int = 900

    REDIS_URL: str = "redis://localhost:6379"
    POSTGRES_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/intervyou_db"

    # SQLAlchemy async engine pool. Defaults left unset previously meant
    # 5 + 10 = 15 connections max for the whole process — worth pinning
    # explicitly once concurrent interview sessions are a real target
    # instead of inheriting whatever SQLAlchemy's own default happens to
    # be.
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    MAX_CV_UPLOAD_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB
    UPLOAD_DIR: Path = BASE_DIR / "uploads" / "cvs"

    # Password-reset delivery. Left empty, forgot_password() falls back to
    # printing the link to the console (dev only) — set all four to send
    # real email via SMTP instead. SMTP_USE_TLS covers STARTTLS (587);
    # leave it on for most providers (Gmail, SES, SendGrid, Mailgun).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_USE_TLS: bool = True

    # Comma-separated list of allowed origins, or "*" for all (dev default).
    # Lock this down to real frontend origin(s) in production.
    CORS_ALLOWED_ORIGINS: str = "*"

    LIVEKIT_API_KEY: str = "devkey"
    LIVEKIT_API_SECRET: str = "secretsecretsecretsecretsecretsecretsecret"
    LIVEKIT_URL: str = "ws://localhost:7880"

    # Shared between the agent worker (app/workers/livekit_agent.py, which
    # registers under this name) and the backend's explicit dispatch call
    # (sessions.py, which targets this exact name) — the single source of
    # truth that keeps the two in sync. Without a name on both sides, the
    # worker only ever receives LiveKit's automatic per-room dispatch,
    # never an explicit one targeted at it by name.
    LIVEKIT_AGENT_NAME: str = "aria-interviewer"

    # OAuth client ID configured in Google Cloud Console for this app.
    # verify_oauth2_token() checks the token's "aud" claim against this
    # value, so a Google ID token minted for a different app can never be
    # replayed against this API.
    GOOGLE_CLIENT_ID: str = ""

    # The interviewer agent's voice pipeline (app/workers/livekit_agent.py)
    # reads these three straight out of os.environ via its own
    # load_dotenv() call — that process never imports this Settings
    # object. They're declared here anyway purely so pydantic-settings
    # (strict by default: model_config has no extra="ignore") doesn't
    # reject the whole .env file — and by extension crash this entire
    # app's startup — the moment .env contains a key this class doesn't
    # know about. Declaring them costs nothing and means "add a new env
    # var" can never again take the whole app down as a side effect.
    DEEPGRAM_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ELEVEN_API_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

settings = Settings()
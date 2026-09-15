from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute, not relative: FastAPI and the Celery worker are separate OS
# processes with different working directories, so a relative upload path
# would resolve differently in each.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    PROJECT_NAME: str = "IntervYou AI Backend"
    API_V1_STR: str = "/api/v1"
    DEBUG: bool = False

    # No default on purpose: a hardcoded fallback would let anyone forge
    # valid JWTs if SECRET_KEY is ever left unset. Missing it must fail startup.
    SECRET_KEY: str
    # Short-lived: a leaked access token can't be revoked before it expires
    # (stateless JWT, signature-only check). Session continuity beyond
    # this is the refresh token's job.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30

    # Refresh-token rotation with token families — see
    # app/auth/services/token_service.py and app/auth/models.py.
    REFRESH_TOKEN_ABSOLUTE_EXPIRE_DAYS: int = 30
    REFRESH_TOKEN_GRACE_PERIOD_SECONDS: int = 5
    REFRESH_TOKEN_COOKIE_NAME: str = "refresh_token"
    # Must be True in production (HTTPS-only), or browsers won't honor
    # the Secure cookie flag.
    COOKIE_SECURE: bool = False

    # Checked alongside the signature on every decode, so a token signed
    # with the right key but minted for a different audience is rejected.
    JWT_ISSUER: str = "intervyou-backend"
    JWT_AUDIENCE: str = "intervyou-app"

    LOGIN_RATE_LIMIT_MAX_ATTEMPTS: int = 5
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 60
    REGISTER_RATE_LIMIT_MAX_ATTEMPTS: int = 5
    REGISTER_RATE_LIMIT_WINDOW_SECONDS: int = 300
    PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS: int = 3
    PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS: int = 300

    # A second counter keyed by email, independent of per-IP limiting
    # above — catches the same account targeted from many IPs.
    ACCOUNT_LOGIN_LOCKOUT_MAX_ATTEMPTS: int = 10
    ACCOUNT_LOGIN_LOCKOUT_WINDOW_SECONDS: int = 900

    REDIS_URL: str = "redis://localhost:6379"
    POSTGRES_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/intervyou_db"

    # SQLAlchemy async engine pool, pinned explicitly rather than left to
    # SQLAlchemy's own defaults.
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    MAX_CV_UPLOAD_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB
    UPLOAD_DIR: Path = BASE_DIR / "uploads" / "cvs"

    # A session whose candidate disconnects without calling /sessions/end
    # would otherwise stay IN_PROGRESS forever, blocked by the
    # one-active-session-per-user constraint. fail_stale_sessions
    # (celery_app.py's beat_schedule) closes those out.
    STALE_SESSION_TIMEOUT_MINUTES: int = 45

    # Left empty, forgot_password() falls back to printing the reset link
    # to the console (dev only) — set all four for real SMTP delivery.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_USE_TLS: bool = True

    # Comma-separated allowed origins, or "*" for all (dev default). Lock
    # this down to real frontend origin(s) in production.
    CORS_ALLOWED_ORIGINS: str = "*"

    LIVEKIT_API_KEY: str = "devkey"
    LIVEKIT_API_SECRET: str = "secretsecretsecretsecretsecretsecretsecret"
    LIVEKIT_URL: str = "ws://localhost:7880"

    # Must match on both sides: the agent worker (livekit_agent.py)
    # registers under this name, and sessions.py's dispatch call targets
    # it by the same name.
    LIVEKIT_AGENT_NAME: str = "aria-interviewer"

    # verify_oauth2_token() checks the token's "aud" claim against this,
    # so a Google ID token minted for a different app can't be replayed here.
    GOOGLE_CLIENT_ID: str = ""

    # Read directly from os.environ by the separate LiveKit agent process
    # (livekit_agent.py), not through this Settings object. Declared here
    # anyway so pydantic-settings (strict by default) doesn't reject the
    # whole .env file over a key this class doesn't recognize.
    DEEPGRAM_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ELEVEN_API_KEY: str = ""
    RIME_API_KEY: str = ""
    SIMLI_API_KEY: str = ""
    SIMLI_FACE_ID: str = ""

    # Which concrete provider class app.interviews.providers.factory returns
    # per role — switching vendors is a config change here, not a code
    # change in VoiceAgent (new vendors still need a class registered in
    # factory.py).
    STT_PROVIDER: str = "deepgram"
    LLM_PROVIDER: str = "gemma"
    TTS_PROVIDER: str = "rime"
    LLM_MODEL: str = "gpt-4o-mini"

    # Unlike the voice-pipeline keys above, DocumentAgent/QuestionnaireAgent
    # run in-process in this app (not the separate LiveKit worker), so this
    # one is read through Settings directly.
    GEMINI_API_KEY: str = ""
    GEMINI_EVAL_API_KEY: str | None = None
    # Comma-separated emails. Registering (password or Google) with one of
    # these is auto-approved and made an admin — the only way to create the
    # first admin, since there's no one to approve them otherwise.
    ADMIN_EMAILS: str = ""

    # Free-tier cap on AI-generated interviews per user (POST
    # /interviews/prepare) — see app.interviews.api.pipeline. Admins
    # bypass this.
    FREE_INTERVIEW_ATTEMPTS: int = 3

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

settings = Settings()
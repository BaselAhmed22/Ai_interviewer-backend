import logging

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from app.core.config import settings
from app.services.redis_service import redis_service

logger = logging.getLogger(__name__)


def _get_client_ip(request: Request) -> str:
    # Behind a reverse proxy or tunnel (Ngrok, Nginx, a load balancer —
    # this project's own docs mention Ngrok specifically), request.client
    # is the proxy's socket, not the real caller. Without this, every user
    # tunneling through the same proxy shares one rate-limit bucket, so one
    # person's failed logins can lock out everyone else behind it.
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _build_rate_limiter(action: str, max_attempts: int, window_seconds: int):
    async def dependency(request: Request) -> None:
        client_ip = _get_client_ip(request)
        key = f"rate-limit:{action}:{client_ip}"
        # Stashed so the endpoint can clear this IP's counter on success —
        # otherwise unrelated failed attempts from a shared IP (NAT, office
        # network) keep counting against a legitimate user's next request.
        request.state.rate_limit_key = key

        try:
            attempts = await redis_service.increment_counter(key, ttl=window_seconds)
        except RedisError:
            # Rate limiting is a defense against abuse, not the core
            # guarantee of these endpoints — a Redis outage must not take
            # down registration/login/password-reset entirely. Fail open
            # and let the request through; the real auth checks (password
            # hash, JWT signature) still apply either way.
            logger.warning("Redis unavailable — allowing request through %s without rate limiting.", action)
            return

        if attempts > max_attempts:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "rate_limited",
                    "message": "Too many attempts. Please try again shortly.",
                },
            )

    return dependency


enforce_login_rate_limit = _build_rate_limiter(
    "login", settings.LOGIN_RATE_LIMIT_MAX_ATTEMPTS, settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS
)
enforce_registration_rate_limit = _build_rate_limiter(
    "register", settings.REGISTER_RATE_LIMIT_MAX_ATTEMPTS, settings.REGISTER_RATE_LIMIT_WINDOW_SECONDS
)
enforce_password_reset_rate_limit = _build_rate_limiter(
    "forgot-password",
    settings.PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS,
    settings.PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS,
)

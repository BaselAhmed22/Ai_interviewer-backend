import logging

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.redis_service import redis_service

logger = logging.getLogger(__name__)


def get_client_ip(request: Request) -> str:
    # Behind a reverse proxy/tunnel, request.client is the proxy's socket,
    # not the real caller — every user behind it would otherwise share one
    # rate-limit bucket.
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _build_rate_limiter(action: str, max_attempts: int, window_seconds: int):
    async def dependency(request: Request) -> None:
        client_ip = get_client_ip(request)
        key = f"rate-limit:{action}:{client_ip}"
        # Stashed so the endpoint can clear this IP's counter on success.
        request.state.rate_limit_key = key

        try:
            attempts = await redis_service.increment_counter(key, ttl=window_seconds)
        except RedisError:
            # Fail open — a Redis outage shouldn't take down auth entirely;
            # the real checks (password hash, JWT signature) still apply.
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

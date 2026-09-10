import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token as google_id_token
from passlib.context import CryptContext

from app.core.config import settings

GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

security_scheme = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def _base_claims() -> dict:
    # iss/aud cost nothing to check today (one service, one audience) but
    # mean a token is only ever valid for *this* backend — if a second
    # service ever shares SECRET_KEY, a token stolen from one can't be
    # replayed against the other just because the signature still checks
    # out.
    return {"iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE}


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {**_base_claims(), "sub": user_id, "type": "access", "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    # jti lets a specific refresh token be revoked (logout) without
    # touching every other token issued to the user — otherwise a leaked
    # refresh token stays usable for its full 7-day lifetime with no way
    # to cut it off early short of rotating SECRET_KEY for everyone.
    payload = {**_base_claims(), "sub": user_id, "type": "refresh", "jti": uuid.uuid4().hex, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def _decode_token_payload(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Could not validate credentials."},
        )

    if payload.get("type") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "wrong_token_type", "message": f"Expected a {expected_type} token."},
        )

    if not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token_payload", "message": "Token payload is missing 'sub'."},
        )
    return payload


def _decode_token(token: str, expected_type: str) -> str:
    return _decode_token_payload(token, expected_type)["sub"]


def decode_user_id(token: str) -> str:
    # Shared by the HTTP dependency below and by the WebSocket handshake in
    # ws_analytics.py, which can't use FastAPI's HTTPBearer the same way.
    return _decode_token(token, expected_type="access")


def decode_refresh_token(token: str) -> tuple[str, str]:
    payload = _decode_token_payload(token, expected_type="refresh")
    return payload["sub"], payload["jti"]


def create_password_reset_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    )
    # A unique jti lets the caller mark this specific token as consumed
    # (in Redis) after it's used once, so a leaked reset link can't be
    # replayed for the rest of its validity window.
    payload = {
        **_base_claims(),
        "sub": user_id,
        "type": "password_reset",
        "jti": uuid.uuid4().hex,
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def decode_password_reset_token(token: str) -> tuple[str, str]:
    payload = _decode_token_payload(token, expected_type="password_reset")
    return payload["sub"], payload["jti"]


def verify_google_id_token(token: str) -> dict:
    # verify_oauth2_token does the real work: checks the RS256 signature
    # against Google's published certs, exp/iat, and — because we pass
    # GOOGLE_CLIENT_ID — that this token was minted for this app's OAuth
    # client and not lifted from some other app's Google sign-in. It also
    # makes a network call (cert fetch, cached) so callers must run this
    # off the event loop.
    try:
        return google_id_token.verify_oauth2_token(
            token, google_auth_requests.Request(), settings.GOOGLE_CLIENT_ID
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_google_token", "message": "Google sign-in token is invalid or expired."},
        ) from exc


def _is_truthy_flag(value) -> bool:
    # The id_token path already hands back a real Python bool for
    # email_verified (parsed from the JWT claim by google-auth). The
    # REST userinfo endpoint used below is documented to do the same, but
    # OAuth userinfo responses across providers are inconsistent enough
    # in practice that treating a stringly "true"/"false" as equivalent
    # costs nothing and avoids silently treating a verified email as
    # unverified over a formatting quirk.
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


async def verify_google_access_token(access_token: str) -> dict:
    # Alternate path for clients whose Google Sign-In SDK hands back an
    # OAuth access token instead of an OIDC id_token (this varies by
    # platform/SDK version). There's no local signature to check here —
    # instead this asks Google's own userinfo endpoint "who does this
    # access token belong to", which only succeeds for a token Google
    # itself still considers valid, live, and unexpired.
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "invalid_google_token", "message": "Could not verify the Google access token."},
            ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_google_token", "message": "Google access token is invalid or expired."},
        )

    data = response.json()
    if not data.get("sub") or not data.get("email"):
        # Google returned 200 but not the shape a userinfo response
        # should have — treat it the same as an invalid token rather
        # than let a KeyError turn this into a 500 further down.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_google_token", "message": "Google access token is invalid or expired."},
        )

    data["email_verified"] = _is_truthy_flag(data.get("email_verified"))
    return data


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> str:
    return decode_user_id(credentials.credentials)
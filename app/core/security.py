import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google.auth.exceptions import GoogleAuthError
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
    # Scopes a token to this backend — a second service sharing SECRET_KEY
    # couldn't have a token replayed against it just from a valid signature.
    return {"iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE}


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {**_base_claims(), "sub": user_id, "type": "access", "exp": expire}
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


def create_password_reset_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    )
    # jti lets the caller mark this specific token consumed in Redis after
    # first use, so a leaked link can't be replayed.
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
    # Checks signature, exp/iat, and (via GOOGLE_CLIENT_ID) that this token
    # was minted for this app. Fetches Google's certs over the network, so
    # callers must run this off the event loop.
    try:
        return google_id_token.verify_oauth2_token(
            token, google_auth_requests.Request(), settings.GOOGLE_CLIENT_ID
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_google_token", "message": "Google sign-in token is invalid or expired."},
        ) from exc
    except GoogleAuthError as exc:
        # Google itself unreachable (network/DNS/outage) — not a
        # ValueError, so it needs its own branch to avoid an unhandled 500.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "google_unreachable", "message": "Could not reach Google right now. Please try again."},
        ) from exc


def _is_truthy_flag(value) -> bool:
    # OAuth userinfo responses aren't consistent about bool vs string here.
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


async def verify_google_access_token(access_token: str) -> dict:
    # For clients whose SDK returns an OAuth access token rather than an
    # id_token — no local signature to check, so ask Google's userinfo
    # endpoint whose token this is.
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
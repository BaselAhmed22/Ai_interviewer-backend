import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.database import engine
from app.api.v1.router import api_router
from app.services.redis_service import redis_service

# Without this, every `logger.info(...)` in this codebase (here and in
# auth.py, rate_limiter.py, tasks.py, ...) is silently dropped: Python's
# root logger defaults to WARNING with no handler attached, and uvicorn's
# own default logging config only sets up its OWN "uvicorn"/"uvicorn.access"
# loggers — it never touches the root logger our app code uses. Verified:
# without this call, logger.info() never reaches the terminal at all;
# only warning/error slip through via Python's WARNING-level "handler of
# last resort". This must run before any other module's logger is used.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is owned entirely by Alembic (`alembic upgrade head`) — this
    # used to also run Base.metadata.create_all() here, but running both
    # let the DB silently drift from the migration history: create_all()
    # only adds missing tables, it never alters an existing one, so a
    # model change without a matching migration would fail at query time
    # instead of at startup, while alembic_version kept claiming the DB
    # was up to date.
    await redis_service.connect()
    print("Redis connection established.")

    yield

    await redis_service.close()
    await engine.dispose()
    print("Database & Redis connections closed gracefully.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

_cors_origins = (
    ["*"]
    if settings.CORS_ALLOWED_ORIGINS.strip() == "*"
    else [origin.strip() for origin in settings.CORS_ALLOWED_ORIGINS.split(",") if origin.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # False is deliberate, not a gap: "credentials" in CORS means
    # cookies/HTTP basic auth/TLS client certs. This API authenticates
    # with a bearer token in the Authorization header, which CORS treats
    # as an ordinary header — already covered by allow_headers=["*"] —
    # and needs no special credentials flag. Turning this on while
    # allow_origins is "*" would also be actively wrong: browsers refuse
    # that combination outright (Access-Control-Allow-Origin: * cannot be
    # paired with Access-Control-Allow-Credentials: true), so it would
    # only break requests, not fix them. None of this applies to a native
    # Android/iOS Flutter build either way — CORS is a browser-only
    # mechanism, enforced by the browser before it lets JS read a
    # response; a native HTTP client (Dio/http package on a phone or
    # emulator) never sends a preflight and never checks these headers at
    # all, so CORS cannot be the cause of a native app failing to reach
    # this server — see the host/port note below instead.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Direct, visible proof of whether a request from the Flutter app is
    # actually arriving at this process at all — the fastest way to tell
    # "network can't reach the server" (nothing logged) apart from "it
    # arrived and something went wrong" (logged, then look at the status
    # code / traceback above).
    started = time.monotonic()
    client_host = request.client.host if request.client else "unknown"
    logger.info("--> %s %s from %s", request.method, request.url.path, client_host)
    response = await call_next(request)
    duration_ms = (time.monotonic() - started) * 1000
    logger.info(
        "<-- %s %s %s (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # exc.errors() can embed non-JSON-serializable objects — e.g. a
    # field_validator that raises ValueError(...) ends up with that
    # exception instance under ctx.error — which crashes plain json.dumps
    # with a 500 instead of returning the 422 this handler exists to
    # produce. jsonable_encoder converts everything to plain JSON types
    # first (dropping what it can't represent, like the raw exception).
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "validation_error",
                "message": "One or more fields are invalid.",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if isinstance(exc.detail, dict):
        code = exc.detail.get("code", "http_error")
        message = exc.detail.get("message", "An error occurred.")
        details = exc.detail.get("details")
    else:
        code = "http_error"
        message = str(exc.detail)
        details = None

    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Last-resort net: anything that reaches here is either a bug or an
    # infrastructure outage (Redis/Postgres unreachable) that no
    # individual endpoint chose to handle specially. A connection-level
    # failure to a dependency we don't control is a 503, not a 500 — and
    # is logged with which dependency failed up front, before the full
    # traceback, so "Postgres is unreachable" vs "Redis is unreachable"
    # vs "there's a bug" is obvious at a glance in the terminal instead of
    # having to read the whole stack trace to find the driver error.
    if isinstance(exc, DBAPIError):
        logger.error("Database unreachable or query failed: %s", exc)
    elif isinstance(exc, RedisError):
        logger.error("Redis unreachable: %s", exc)
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)

    if isinstance(exc, RedisError) or isinstance(exc, DBAPIError):
        response = JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "service_unavailable",
                    "message": "A required service is temporarily unavailable. Please try again shortly.",
                    "details": None,
                }
            },
        )
    else:
        response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred. Please try again.",
                    "details": None,
                }
            },
        )

    # This handler runs in Starlette's ServerErrorMiddleware, which sits
    # OUTSIDE (above) CORSMiddleware — so a response built here never
    # passes back through CORSMiddleware and would otherwise reach the
    # browser with no Access-Control-Allow-Origin header at all. A
    # browser-based client (Flutter web included) then can't read the
    # response body or status — it just sees an opaque CORS/network
    # failure, hiding the real 500/503 and message from the app entirely.
    # Every other exception handler in this file (RequestValidationError,
    # StarletteHTTPException) runs inside ExceptionMiddleware, which is
    # nested inside CORSMiddleware, so only this one needs the header
    # added by hand. Matches this app's actual CORS policy below
    # (wildcard origin, no credentials) — update both together if that
    # policy ever changes.
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.get("/", tags=["Health"])
async def root_health_check():
    return {"status": "online", "project": settings.PROJECT_NAME, "version": "1.0.0"}


app.include_router(api_router, prefix=settings.API_V1_STR)


if __name__ == "__main__":
    # `uvicorn app.main:app --reload` (the command this project's README
    # documented) binds to 127.0.0.1 by default — reachable only from this
    # same machine. A phone, emulator, or anything going through ngrok
    # connects from a different address entirely, so every one of their
    # requests gets refused at the TCP level before this app ever sees
    # them (indistinguishable, from the Flutter side, from the server
    # being down — hence "Unable to reach backend server" with nothing
    # logged here at all). `python -m app.main` now binds 0.0.0.0 without
    # depending on anyone remembering the right flags.
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.DEBUG)
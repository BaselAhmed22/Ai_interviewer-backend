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
from app.api.router import api_router
from app.core.redis_service import redis_service

# Python's root logger defaults to WARNING with no handler attached, and
# uvicorn's own logging config only sets up its "uvicorn"/"uvicorn.access"
# loggers — without this, every logger.info() across the app is silently
# dropped. Must run before any other module's logger is used.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is owned entirely by Alembic (`alembic upgrade head`) — no
    # create_all() here, since that only adds missing tables and never
    # alters existing ones, letting the DB silently drift from migrations.
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
    # False is deliberate: this API authenticates via a bearer token in
    # the Authorization header, not cookies, so "credentials" don't apply
    # — and it's incompatible with allow_origins="*" anyway (browsers
    # reject that combination).
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
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
    # exc.errors() can embed non-JSON-serializable objects (e.g. a raised
    # exception under ctx.error) — jsonable_encoder strips those instead
    # of crashing json.dumps with a 500.
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
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Last-resort net: anything reaching here is either a bug or an
    # infrastructure outage. Log which dependency failed up front, before
    # the full traceback, and return 503 (not 500) for outages.
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
    # outside CORSMiddleware — unlike every other handler in this file, a
    # response built here never passes back through CORSMiddleware, so
    # the header has to be added by hand. Keep this in sync with the CORS
    # policy above (wildcard origin, no credentials).
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.get("/", tags=["Health"])
async def root_health_check():
    return {"status": "online", "project": settings.PROJECT_NAME, "version": "1.0.0"}


app.include_router(api_router, prefix=settings.API_V1_STR)


if __name__ == "__main__":
    # `uvicorn app.main:app --reload` binds 127.0.0.1 by default, unreachable
    # from a phone, emulator, or ngrok tunnel. Binding 0.0.0.0 here avoids
    # depending on anyone remembering the right flags.
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.DEBUG)
# PostgreSQL Async Setup
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.core.config import settings

engine = create_async_engine(
    settings.POSTGRES_URL,
    echo=settings.DEBUG,
    future=True,
    pool_timeout=10,
    # Explicit instead of SQLAlchemy's defaults (5 + 10 = 15 total) — with
    # concurrent interview sessions each holding a connection for the
    # duration of a request, the default ceiling is easy to exhaust under
    # real concurrent load. Tune via DB_POOL_SIZE/DB_MAX_OVERFLOW rather
    # than code changes as that number becomes clearer under load.
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # Without this, a hung query (lock contention, a stalled connection)
    # blocks the request that issued it for as long as the OS-level TCP
    # timeout allows — effectively unbounded. asyncpg's command_timeout
    # caps any single query/statement at 10s instead.
    connect_args={"command_timeout": 10},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
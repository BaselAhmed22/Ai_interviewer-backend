import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# --------------------------------------------------------------------------
# Import settings and the full model registry BEFORE anything else runs.
#
# `app.db.base` is the file that imports every single ORM model (User,
# CandidateProfile, JobDescription, InterviewPreference, InterviewSession,
# InterviewReport). Importing it here — not just `Base` from
# `app.core.database` — is what makes autogenerate actually "see" every
# table. env.py is a standalone entrypoint that never goes through
# `main.py`'s import chain, so if a model isn't imported here explicitly,
# Alembic has no way of knowing it exists.
# --------------------------------------------------------------------------
from app.core.config import settings
from app.core.db_registry import Base  # noqa: F401  (import side effect registers all models)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    # Single source of truth for both offline and online paths — make
    # sure this attribute name matches your Settings class exactly
    # (this project's config.py calls it POSTGRES_URL).
    return settings.POSTGRES_URL


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode using an async engine — required
    because POSTGRES_URL uses the asyncpg driver
    (postgresql+asyncpg://...), which only works with SQLAlchemy's async
    engine API, not the classic synchronous `engine_from_config`.
    """
    # Build the config section dict manually and inject the real URL
    # directly into it, rather than calling config.set_main_option(...)
    # and relying on get_section() picking it up. This removes any
    # dependency on alembic.ini's [alembic] section, ConfigParser
    # interpolation, or section-name timing — the driver://... placeholder
    # in alembic.ini is never read at all with this approach.
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
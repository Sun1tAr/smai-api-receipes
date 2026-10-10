import asyncio

import structlog
from alembic import context
from asyncpg import PostgresError
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from receipes.config import Settings
from receipes.database import Base, create_engine
from receipes.observability import configure_logging

settings = Settings()
configure_logging(settings.log_level)
# Доменные модели появятся позже; их модуль нужно импортировать здесь.
target_metadata = Base.metadata


# Offline-режим генерирует SQL без подключения к PostgreSQL.
def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# Alembic выполняет синхронные операции через async-соединение SQLAlchemy.
def migrate(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# Подключение миграций использует ту же конфигурацию, что и приложение.
async def run_migrations_online() -> None:
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    except (OSError, PostgresError, SQLAlchemyError) as error:
        structlog.get_logger().error(
            "migration_failed", error_type=type(error).__name__
        )
        raise SystemExit(1) from None
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

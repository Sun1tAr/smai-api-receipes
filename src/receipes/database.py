from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from receipes.config import Settings


# Явные имена ограничений делают миграции воспроизводимыми и позволяют их распознавать.
class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


# Ограничиваем ожидание БД, чтобы проверка готовности не зависала.
def create_engine(settings: Settings) -> AsyncEngine:
    return _create_engine(settings, command_timeout=settings.db_timeout_seconds)


# Миграции не ограничиваются таймаутом запросов приложения: DDL на большой таблице
# идёт дольше. Ожидание подключения ограничено так же, как у приложения.
def create_migration_engine(settings: Settings) -> AsyncEngine:
    return _create_engine(settings, command_timeout=None)


def _create_engine(settings: Settings, *, command_timeout: float | None) -> AsyncEngine:
    connect_args: dict[str, float] = {"timeout": settings.db_timeout_seconds}
    if command_timeout is not None:
        connect_args["command_timeout"] = command_timeout
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=settings.db_timeout_seconds,
        hide_parameters=True,
        connect_args=connect_args,
    )

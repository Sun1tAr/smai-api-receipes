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
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=settings.db_timeout_seconds,
        hide_parameters=True,
        connect_args={
            "timeout": settings.db_timeout_seconds,
            "command_timeout": settings.db_timeout_seconds,
        },
    )

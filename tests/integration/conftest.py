import os
import subprocess
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.config import Settings
from receipes.database import Base, create_engine
from receipes.db import models  # noqa: F401


# Интеграционные тесты подключаются только к отдельной тестовой БД.
@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        db_host=os.environ["TEST_DB_HOST"],
        db_port=int(os.getenv("TEST_DB_PORT", "5432")),
        db_name=os.environ["TEST_DB_NAME"],
        db_user=os.environ["TEST_DB_USER"],
        db_password=os.environ["TEST_DB_PASSWORD"],
        ollama_base_url="http://ollama.test:11434",
    )


# Alembic читает конфигурацию из окружения, как при запуске контейнера.
@pytest.fixture
def alembic_env(settings: Settings) -> dict[str, str]:
    return {
        **os.environ,
        "DB_HOST": settings.db_host,
        "DB_PORT": str(settings.db_port),
        "DB_NAME": settings.db_name,
        "DB_USER": settings.db_user,
        "DB_PASSWORD": settings.db_password.get_secret_value(),
        "OLLAMA_BASE_URL": str(settings.ollama_base_url),
    }


def run_alembic(env: dict[str, str], *arguments: str) -> None:
    result = subprocess.run(
        ["alembic", *arguments], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Схема на уровне head; тест с временной ревизией перед собой откатывает её до base.
@pytest.fixture
def schema(alembic_env: dict[str, str]) -> None:
    run_alembic(alembic_env, "upgrade", "head")


# Пустая БД без версии миграций: для тестов с временной веткой ревизий.
@pytest.fixture
def empty_schema(alembic_env: dict[str, str]) -> None:
    run_alembic(alembic_env, "downgrade", "base")


# Чистая тестовая БД: таблицы домена очищены перед каждым тестом.
@pytest_asyncio.fixture
async def session_factory(
    settings: Settings, schema: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(Base.metadata.tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield factory
    await engine.dispose()

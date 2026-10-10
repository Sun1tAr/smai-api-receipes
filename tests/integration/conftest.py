import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from receipes.config import Settings


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


def _alembic_run(env: dict[str, str], *arguments: str) -> None:
    result = subprocess.run(
        ["alembic", *arguments], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# Схема применяется один раз за сессию; тесты миграций возвращают её к head.
@pytest.fixture(scope="session")
def migrated_database() -> None:
    if not os.getenv("TEST_DB_HOST"):
        pytest.skip("TEST_DB_HOST is not set")
    env = {
        **os.environ,
        "DB_HOST": os.environ["TEST_DB_HOST"],
        "DB_PORT": os.getenv("TEST_DB_PORT", "5432"),
        "DB_NAME": os.environ["TEST_DB_NAME"],
        "DB_USER": os.environ["TEST_DB_USER"],
        "DB_PASSWORD": os.environ["TEST_DB_PASSWORD"],
        "OLLAMA_BASE_URL": "http://ollama.test:11434",
    }
    _alembic_run(env, "upgrade", "head")


TABLES = (
    "generation_task, generation_parameters, menu_recipes, menu, recipe_ingredients, "
    "instruction, recipe, nutrition, ingredient, prompt_template"
)


@pytest.fixture
async def session_factory(settings: Settings, migrated_database: None):
    from receipes.database import create_engine

    engine = create_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(session_factory):
    async with session_factory() as session:
        yield session

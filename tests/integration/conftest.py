import os

import pytest

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

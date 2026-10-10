from pathlib import Path

import pytest
from pydantic import ValidationError


def required_settings(**extra: object) -> dict[str, object]:
    return {
        "db_host": "postgres",
        "db_name": "receipes",
        "db_user": "receipes",
        "db_password": "local-only",
        "ollama_base_url": "http://ollama.test:11434",
        **extra,
    }


def make(**extra: object):
    from receipes.config import Settings

    return Settings(_env_file=None, **required_settings(**extra))


def test_missing_required_settings_prevent_startup() -> None:
    from receipes.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "missing", ["db_host", "db_name", "db_user", "db_password", "ollama_base_url"]
)
def test_each_required_setting_is_required(missing: str) -> None:
    from receipes.config import Settings

    values = required_settings()
    del values[missing]
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **values)
    assert missing in str(error.value)


def test_defaults() -> None:
    settings = make()
    assert settings.db_port == 5432
    assert settings.db_timeout_seconds == 5
    assert settings.log_level == "INFO"
    assert settings.otel_exporter_otlp_endpoint is None
    assert settings.ollama_model == "qwen2.5:7b-instruct-q4_K_M"
    assert settings.ollama_connect_timeout_seconds == 5
    assert settings.ollama_timeout_seconds == 300
    assert settings.ollama_num_ctx == 32768
    assert settings.ollama_num_predict == 4096
    assert settings.ollama_temperature == 0.3
    assert settings.ollama_keep_alive == "30m"
    assert settings.generation_worker_enabled is True
    assert settings.generation_worker_interval_seconds == 2
    assert settings.generation_pending_ttl_seconds == 900
    assert settings.generation_max_pending is None
    assert settings.generation_bad_response_retries == 1


def test_environment_overrides_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    from receipes.config import Settings

    Path(".env").write_text(
        "DB_HOST=postgres\nDB_NAME=receipes\nDB_USER=receipes\n"
        "DB_PASSWORD=local-only\nOLLAMA_BASE_URL=http://ollama.test:11434\n"
    )
    monkeypatch.setenv("DB_HOST", "deployment-db")
    settings = Settings()
    assert settings.db_host == "deployment-db"
    assert settings.db_name == "receipes"


def test_extra_environment_is_ignored() -> None:
    from receipes.config import Settings

    Path(".env").write_text(
        "DB_HOST=postgres\nDB_NAME=receipes\nDB_USER=receipes\n"
        "DB_PASSWORD=local-only\nOLLAMA_BASE_URL=http://ollama.test:11434\n"
        "RECEIPES_HTTP_PORT=8001\nUNKNOWN=1\n"
    )
    assert Settings().db_host == "postgres"


def test_empty_values_in_dotenv_mean_unset() -> None:
    from receipes.config import Settings

    Path(".env").write_text(
        "DB_HOST=postgres\nDB_NAME=receipes\nDB_USER=receipes\n"
        "DB_PASSWORD=local-only\nOLLAMA_BASE_URL=http://ollama.test:11434\n"
        "DB_PORT=\nDB_TIMEOUT_SECONDS=\nLOG_LEVEL=\nOTEL_EXPORTER_OTLP_ENDPOINT=\n"
        "OLLAMA_MODEL=\nOLLAMA_NUM_CTX=\nOLLAMA_KEEP_ALIVE=\n"
        "GENERATION_WORKER_ENABLED=\nGENERATION_MAX_PENDING=\n"
        "GENERATION_BAD_RESPONSE_RETRIES=\n"
    )
    settings = Settings()
    assert settings.db_port == 5432
    assert settings.db_timeout_seconds == 5
    assert settings.log_level == "INFO"
    assert settings.otel_exporter_otlp_endpoint is None
    assert settings.ollama_model == "qwen2.5:7b-instruct-q4_K_M"
    assert settings.ollama_num_ctx == 32768
    assert settings.ollama_keep_alive == "30m"
    assert settings.generation_worker_enabled is True
    assert settings.generation_max_pending is None
    assert settings.generation_bad_response_retries == 1


def test_empty_required_environment_value_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from receipes.config import Settings

    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_NAME", "receipes")
    monkeypatch.setenv("DB_USER", "receipes")
    monkeypatch.setenv("DB_PASSWORD", "local-only")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_values_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from receipes.config import Settings

    for key, value in {
        "DB_HOST": "postgres",
        "DB_NAME": "receipes",
        "DB_USER": "receipes",
        "DB_PASSWORD": "local-only",
        "OLLAMA_BASE_URL": "http://ollama.test:11434",
        "OLLAMA_MODEL": "other:model",
        "OLLAMA_TEMPERATURE": "0.7",
        "GENERATION_WORKER_ENABLED": "false",
        "GENERATION_MAX_PENDING": "10",
        "GENERATION_BAD_RESPONSE_RETRIES": "0",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.ollama_model == "other:model"
    assert settings.ollama_temperature == 0.7
    assert settings.generation_worker_enabled is False
    assert settings.generation_max_pending == 10
    assert settings.generation_bad_response_retries == 0


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert make().log_level == "DEBUG"


@pytest.mark.parametrize(
    "override",
    [
        {"db_port": 0},
        {"db_port": 65536},
        {"db_timeout_seconds": 0},
        {"db_timeout_seconds": 61},
        {"log_level": "TRACE"},
        {"ollama_base_url": "ollama.test:11434"},
        {"ollama_base_url": "ftp://ollama.test"},
        {"ollama_model": ""},
        {"ollama_connect_timeout_seconds": 0},
        {"ollama_timeout_seconds": 0},
        {"ollama_timeout_seconds": 3601},
        {"ollama_num_ctx": 0},
        {"ollama_num_predict": 0},
        {"ollama_temperature": -0.1},
        {"ollama_temperature": 2.1},
        {"generation_worker_interval_seconds": 0},
        {"generation_pending_ttl_seconds": 0},
        {"generation_max_pending": 0},
        {"generation_bad_response_retries": -1},
        {"generation_bad_response_retries": 11},
    ],
)
def test_out_of_range_values_are_rejected(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make(**override)


def test_database_url_preserves_special_characters_without_exposing_password() -> None:
    secret = "local@:/%?#password"
    settings = make(db_password=secret)
    assert settings.database_url.password == secret
    assert settings.database_url.drivername == "postgresql+asyncpg"
    assert secret not in repr(settings)
    assert secret not in str(settings.database_url)


def test_validation_error_does_not_echo_secret() -> None:
    secret = "test-secret-that-must-not-appear"
    with pytest.raises(ValidationError) as error:
        make(db_password=secret, db_port="invalid")
    assert secret not in str(error.value)

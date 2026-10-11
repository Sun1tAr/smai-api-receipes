from pathlib import Path

import pytest


# Ни .env разработчика, ни переменные процесса не должны влиять на unit-тесты.
@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in (
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
        "DB_TIMEOUT_SECONDS",
        "MIGRATION_LOCK_TIMEOUT_SECONDS",
        "LOG_LEVEL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OLLAMA_BASE_URL",
        "OLLAMA_MODEL",
        "OLLAMA_CONNECT_TIMEOUT_SECONDS",
        "OLLAMA_TIMEOUT_SECONDS",
        "OLLAMA_NUM_CTX",
        "OLLAMA_NUM_PREDICT",
        "OLLAMA_TEMPERATURE",
        "OLLAMA_KEEP_ALIVE",
        "GENERATION_WORKER_ENABLED",
        "GENERATION_WORKER_INTERVAL_SECONDS",
        "GENERATION_PENDING_TTL_SECONDS",
        "GENERATION_MAX_PENDING",
        "GENERATION_BAD_RESPONSE_RETRIES",
        "GENERATION_STANDBY_INTERVAL_SECONDS",
        "GENERATION_LOCK_KEEPALIVE_SECONDS",
        "GENERATION_BREAKER_MIN_PAUSE_SECONDS",
        "GENERATION_BREAKER_MAX_PAUSE_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)

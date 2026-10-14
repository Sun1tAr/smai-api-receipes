from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# Запас контекста сверх OLLAMA_NUM_PREDICT на сам промпт: ~820 токенов измерено
# (SMAI-54) плюс запас на рост шаблона.
CONTEXT_RESERVE_TOKENS = 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Пустое значение (KEY= в .env или ${KEY:-} в Compose) означает «не задано».
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    db_host: str = Field(min_length=1)
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: SecretStr = Field(min_length=1)
    db_timeout_seconds: float = Field(default=5, gt=0, le=60)
    # Ожидание блокировки при миграциях (только Alembic): чужая блокировка не должна
    # подвешивать старт контейнера бесконечно.
    migration_lock_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None

    ollama_base_url: AnyHttpUrl
    ollama_model: str = Field(default="qwen2.5:7b", min_length=1)
    ollama_connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    ollama_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    # 8192 целиком в видеопамяти; 32768 — нет, и это в 2,6 раза медленнее.
    ollama_num_ctx: int = Field(default=8192, ge=512, le=1_048_576)
    ollama_num_predict: int = Field(default=4096, ge=1, le=1_048_576)
    ollama_temperature: float = Field(default=0.3, ge=0, le=2)
    ollama_keep_alive: str = Field(default="30m", min_length=1)

    # Фоновая генерация меню.
    generation_worker_enabled: bool = True
    generation_worker_interval_seconds: float = Field(default=2, gt=0, le=3600)
    # Повторы только при невалидном ответе модели (не при таймауте и обрезке).
    generation_bad_response_retries: int = Field(default=1, ge=0, le=10)

    # LOG_LEVEL=info допустим: регистр в окружении легко перепутать.
    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    # Пробельное значение так же бессмысленно, как пустое. Само значение не меняем:
    # пробелы могут быть частью пароля.
    @field_validator("db_host", "db_name", "db_user", mode="after")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("db_password", mode="after")
    @classmethod
    def reject_blank_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    # Ответ модели должен помещаться в контекст вместе с промптом.
    @model_validator(mode="after")
    def check_context_fits_output(self) -> "Settings":
        if self.ollama_num_predict + CONTEXT_RESERVE_TOKENS > self.ollama_num_ctx:
            raise ValueError(
                f"OLLAMA_NUM_PREDICT + {CONTEXT_RESERVE_TOKENS} must not exceed "
                f"OLLAMA_NUM_CTX (got {self.ollama_num_predict} and "
                f"{self.ollama_num_ctx})"
            )
        return self

    # Учётные данные в URL попали бы в логи, трейсы и сообщения об ошибках.
    @field_validator("ollama_base_url", "otel_exporter_otlp_endpoint", mode="after")
    @classmethod
    def reject_url_credentials(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and (value.username or value.password):
            raise ValueError("must not contain credentials (user:password@host)")
        return value

    # URL.create сохраняет спецсимволы пароля без ручного экранирования.
    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

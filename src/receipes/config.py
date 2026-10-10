from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


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

    # Ollama: клиент появится позже, пока задаётся только конфигурация.
    ollama_base_url: AnyHttpUrl
    ollama_model: str = Field(default="qwen2.5:7b-instruct-q4_K_M", min_length=1)
    ollama_connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    ollama_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    ollama_num_ctx: int = Field(default=32768, ge=512, le=1_048_576)
    ollama_num_predict: int = Field(default=4096, ge=1, le=1_048_576)
    ollama_temperature: float = Field(default=0.3, ge=0, le=2)
    ollama_keep_alive: str = Field(default="30m", min_length=1)

    # Фоновая генерация меню: воркер появится позже.
    generation_worker_enabled: bool = True
    generation_worker_interval_seconds: float = Field(default=2, gt=0, le=3600)
    generation_pending_ttl_seconds: int = Field(default=900, ge=1, le=86400)
    # Не задан — очередь без лимита.
    generation_max_pending: int | None = Field(default=None, ge=1)
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

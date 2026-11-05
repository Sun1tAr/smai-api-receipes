from typing import Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# Запас контекста сверх OLLAMA_NUM_PREDICT на сам промпт: ~820 токенов шаблона
# (SMAI-54) плюс блок ограничений v2 (SMAI-89: худший случай по сиду MS-2
# +334 токена по замеру, всего 1156) плюс запас на рост.
CONTEXT_RESERVE_TOKENS = 1536


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

    # Справочники MS-2 (API-7): MS-4 проверяет по ним ID из запроса API-1. Адрес
    # обязателен; таймаут действует и на каждую фазу запроса, и на запрос целиком.
    ms2_base_url: AnyHttpUrl
    ms2_timeout_seconds: float = Field(default=5, gt=0, le=60)

    # MCP ВкусВилла (API-14). Без адреса остальные API остаются доступны.
    vkusvill_mcp_url: AnyHttpUrl | None = None
    mcp_call_timeout_seconds: float = Field(default=5, gt=0, le=5)
    mcp_call_retries: int = Field(default=1, ge=0, le=1)
    mcp_concurrency: int = Field(default=4, ge=1, le=4)
    mcp_cache_ttl_seconds: float = Field(default=900, ge=0, le=900)
    cart_deadline_seconds: float = Field(default=35, gt=0, le=35)

    # Фоновая генерация меню.
    generation_worker_enabled: bool = True
    generation_worker_interval_seconds: float = Field(default=2, gt=0, le=3600)
    # Как часто обновляются gauge очереди генерации в /metrics.
    generation_metrics_refresh_seconds: float = Field(default=15, gt=0, le=300)
    # Повторы только при невалидном ответе модели (не при таймауте и обрезке).
    generation_bad_response_retries: int = Field(default=1, ge=0, le=10)
    replace_wait_seconds: float = Field(default=120, gt=0, le=175)

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

    # Ответ модели должен помещаться в контекст вместе с промптом. Проверка на поле
    # num_predict (оно объявлено после num_ctx): так в лог invalid_configuration
    # попадают имя поля и понятный тип ошибки.
    @field_validator("ollama_num_predict", mode="after")
    @classmethod
    def check_output_fits_context(cls, value: int, info: ValidationInfo) -> int:
        num_ctx = info.data.get("ollama_num_ctx")
        if num_ctx is not None and value + CONTEXT_RESERVE_TOKENS > num_ctx:
            raise PydanticCustomError(
                "num_predict_exceeds_context",
                "OLLAMA_NUM_PREDICT + {reserve} must not exceed OLLAMA_NUM_CTX "
                "(got {num_predict} and {num_ctx})",
                {
                    "reserve": CONTEXT_RESERVE_TOKENS,
                    "num_predict": value,
                    "num_ctx": num_ctx,
                },
            )
        return value

    # Учётные данные в URL попали бы в логи, трейсы и сообщения об ошибках.
    @field_validator(
        "ollama_base_url",
        "otel_exporter_otlp_endpoint",
        "ms2_base_url",
        "vkusvill_mcp_url",
        mode="after",
    )
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

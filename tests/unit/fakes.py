from receipes.config import Settings


# Порт 1 заведомо закрыт: БД «недоступна» без обращения к сети.
def unavailable_db_settings(**extra: object) -> Settings:
    values: dict[str, object] = {
        "db_host": "127.0.0.1",
        "db_port": 1,
        "db_user": "receipes",
        "db_name": "receipes",
        "db_password": "private-test-password",
        "ollama_base_url": "http://ollama.test:11434",
        "db_timeout_seconds": 1,
        **extra,
    }
    return Settings(_env_file=None, **values)

from typing import Any

from asyncpg import PostgresError


# Ищем исключение драйвера среди обёрток SQLAlchemy (orig) и причин (__cause__).
def find_postgres_error(error: BaseException) -> PostgresError | None:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PostgresError):
            return current
        for linked in (getattr(current, "orig", None), current.__cause__):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return None


# Только безопасные поля: тип, SQLSTATE и основное сообщение сервера БД. Текст
# обёрток SQLAlchemy (SQL, параметры) и DETAIL/HINT PostgreSQL не включаются.
def describe_migration_error(error: BaseException) -> dict[str, Any]:
    postgres_error = find_postgres_error(error)
    fields: dict[str, Any] = {
        "error_type": type(error).__name__,
        "sqlstate": getattr(postgres_error, "sqlstate", None),
    }
    if postgres_error is not None:
        fields["driver_error_type"] = type(postgres_error).__name__
        fields["db_message"] = getattr(postgres_error, "message", None)
    return fields

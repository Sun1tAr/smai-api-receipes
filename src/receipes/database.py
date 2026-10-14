import socket
from typing import Any

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from receipes.config import Settings


# Явные имена ограничений делают миграции воспроизводимыми и позволяют их распознавать.
class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


# Ограничиваем ожидание БД, чтобы проверка готовности не зависала.
def create_engine(settings: Settings) -> AsyncEngine:
    return _create_engine(settings, command_timeout=settings.db_timeout_seconds)


# Миграции не ограничиваются таймаутом запросов приложения: DDL на большой таблице
# идёт дольше. Ожидание подключения ограничено так же, как у приложения, а ожидание
# чужой блокировки — lock_timeout.
def create_migration_engine(settings: Settings) -> AsyncEngine:
    lock_timeout_ms = round(settings.migration_lock_timeout_seconds * 1000)
    return _create_engine(
        settings,
        command_timeout=None,
        server_settings={"lock_timeout": f"{lock_timeout_ms}ms"},
    )


# Соединение с advisory lock живёт часами. Серверные keepalive (GUC сессии) освободят
# замок, если клиент пропал: мёртвый сокет обнаружится через idle + interval * count
# (30 + 10 * 3 = 60 с). Те же параметры ставятся и на клиентский сокет, чтобы мы
# заметили пропавший сервер, а не ждали вечно.
KEEPALIVE_IDLE_SECONDS = 30
KEEPALIVE_INTERVAL_SECONDS = 10
KEEPALIVE_COUNT = 3


def _enable_client_keepalive(dbapi_connection: Any, _record: Any) -> None:
    transport = getattr(dbapi_connection.driver_connection, "_transport", None)
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None:
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    idle = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", None))
    for option, value in (
        (idle, KEEPALIVE_IDLE_SECONDS),
        (getattr(socket, "TCP_KEEPINTVL", None), KEEPALIVE_INTERVAL_SECONDS),
        (getattr(socket, "TCP_KEEPCNT", None), KEEPALIVE_COUNT),
    ):
        if option is not None:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)


# Отдельный движок для замка воркера: одно соединение с TCP keepalive.
def create_lock_engine(settings: Settings) -> AsyncEngine:
    engine = _create_engine(
        settings,
        command_timeout=settings.db_timeout_seconds,
        server_settings={
            "tcp_keepalives_idle": str(KEEPALIVE_IDLE_SECONDS),
            "tcp_keepalives_interval": str(KEEPALIVE_INTERVAL_SECONDS),
            "tcp_keepalives_count": str(KEEPALIVE_COUNT),
        },
    )
    event.listen(engine.sync_engine, "connect", _enable_client_keepalive)
    return engine


def _create_engine(
    settings: Settings,
    *,
    command_timeout: float | None,
    server_settings: dict[str, str] | None = None,
) -> AsyncEngine:
    connect_args: dict[str, Any] = {"timeout": settings.db_timeout_seconds}
    if command_timeout is not None:
        connect_args["command_timeout"] = command_timeout
    if server_settings:
        connect_args["server_settings"] = server_settings
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=settings.db_timeout_seconds,
        hide_parameters=True,
        connect_args=connect_args,
    )

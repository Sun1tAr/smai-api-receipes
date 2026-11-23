import pytest
from fakes import unavailable_db_settings


@pytest.fixture
def engine_kwargs(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    import receipes.database as database

    calls: list[dict] = []
    monkeypatch.setattr(
        database, "create_async_engine", lambda url, **kwargs: calls.append(kwargs)
    )
    return calls


def test_application_engine_limits_connections_and_statements(engine_kwargs) -> None:
    from receipes.database import create_engine

    create_engine(unavailable_db_settings(db_timeout_seconds=3))
    # lock_timeout относится только к миграциям: запросы приложения не меняются.
    assert engine_kwargs[0]["connect_args"] == {"timeout": 3, "command_timeout": 3}


def test_migration_lock_timeout_is_configurable(engine_kwargs) -> None:
    from receipes.database import create_migration_engine

    create_migration_engine(unavailable_db_settings(migration_lock_timeout_seconds=1.5))
    settings = engine_kwargs[0]["connect_args"]["server_settings"]
    assert settings == {"lock_timeout": "1500ms"}


def test_migration_engine_limits_only_the_connection(engine_kwargs) -> None:
    from receipes.database import create_migration_engine

    # DDL на большой таблице может идти дольше таймаута запросов приложения.
    create_migration_engine(unavailable_db_settings(db_timeout_seconds=3))
    assert engine_kwargs[0]["connect_args"] == {
        "timeout": 3,
        "server_settings": {"lock_timeout": "30000ms"},
    }
    assert engine_kwargs[0]["hide_parameters"] is True


class BrokenTransport:
    def get_extra_info(self, name: str) -> object:
        raise OSError("socket is gone")


class FakeDriverConnection:
    _transport = BrokenTransport()


class FakeDbapiConnection:
    driver_connection = FakeDriverConnection()


def test_keepalive_failure_does_not_break_the_connection_and_warns_once(
    capsys: "pytest.CaptureFixture[str]", monkeypatch: "pytest.MonkeyPatch"
) -> None:
    import json

    from receipes import database
    from receipes.observability import configure_logging

    configure_logging("INFO")
    monkeypatch.setattr(database, "_keepalive_warned", False)
    for _ in range(3):
        database._enable_client_keepalive(FakeDbapiConnection(), None)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    warnings = [
        item
        for item in map(json.loads, lines)
        if item["message"] == "client_keepalive_not_applied"
    ]
    assert len(warnings) == 1
    assert warnings[0]["level"] == "warning" and warnings[0]["error_type"] == "OSError"


def test_keepalive_without_driver_internals_is_a_noop() -> None:
    from receipes import database

    class Bare:
        driver_connection = object()

    database._enable_client_keepalive(Bare(), None)

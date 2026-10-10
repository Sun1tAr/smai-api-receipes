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
    assert engine_kwargs[0]["connect_args"] == {"timeout": 3, "command_timeout": 3}


def test_migration_engine_limits_only_the_connection(engine_kwargs) -> None:
    from receipes.database import create_migration_engine

    # DDL на большой таблице может идти дольше таймаута запросов приложения.
    create_migration_engine(unavailable_db_settings(db_timeout_seconds=3))
    assert engine_kwargs[0]["connect_args"] == {"timeout": 3}
    assert engine_kwargs[0]["hide_parameters"] is True

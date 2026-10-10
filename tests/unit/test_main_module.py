import pytest


def test_server_listens_on_all_interfaces_port_8000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import receipes.__main__ as entrypoint

    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        entrypoint.uvicorn, "run", lambda app, **options: calls.append((app, options))
    )
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_NAME", "receipes")
    monkeypatch.setenv("DB_USER", "receipes")
    monkeypatch.setenv("DB_PASSWORD", "local-only")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    entrypoint.main()
    options = calls[0][1]
    assert options["host"] == "0.0.0.0"
    assert options["port"] == 8000
    assert options["access_log"] is False

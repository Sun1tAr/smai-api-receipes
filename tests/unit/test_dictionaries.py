import httpx
import pytest
from fakes import unavailable_db_settings

from receipes.dictionaries import DictionaryClient
from receipes.domain.errors import DictionaryError

PAYLOAD = {
    "habits": [{"id": 1, "name": "Быстрые перекусы"}],
    "diets": [{"id": 4, "name": "Средиземноморская"}],
    "allergies": [{"id": 1, "name": "Глютен"}, {"id": 2, "name": "Лактоза"}],
    "kitchenTools": [{"id": 6, "name": "Блендер"}],
}


def client_for(handler) -> DictionaryClient:
    settings = unavailable_db_settings(ms2_base_url="http://ms2.test:8000")
    return DictionaryClient(
        DictionaryClient.build_http_client(settings, httpx.MockTransport(handler))
    )


async def test_fetch_returns_names_by_id() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=PAYLOAD)

    dictionaries = await client_for(handler).fetch()
    assert seen == ["http://ms2.test:8000/dictionaries"]
    assert dictionaries.habits == {1: "Быстрые перекусы"}
    assert dictionaries.diets == {4: "Средиземноморская"}
    assert dictionaries.allergies == {1: "Глютен", 2: "Лактоза"}
    assert dictionaries.kitchen_tools == {6: "Блендер"}


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (lambda r: httpx.Response(500), "Dictionaries error"),
        (lambda r: httpx.Response(404), "Dictionaries error"),
        (
            lambda r: httpx.Response(200, text="not json"),
            "Invalid dictionaries response",
        ),
        (
            lambda r: httpx.Response(200, json={"habits": []}),
            "Invalid dictionaries response",
        ),
        (
            lambda r: httpx.Response(200, json={**PAYLOAD, "diets": [{"id": "x"}]}),
            "Invalid dictionaries response",
        ),
    ],
)
async def test_bad_answers_become_dictionary_errors(handler, message: str) -> None:
    with pytest.raises(DictionaryError) as error:
        await client_for(handler).fetch()
    assert error.value.message == message


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (httpx.ReadTimeout("secret-host"), "Dictionaries timeout"),
        (httpx.ConnectError("secret-host"), "Dictionaries unavailable"),
    ],
)
async def test_network_failures_have_safe_messages(failure, message: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    with pytest.raises(DictionaryError) as error:
        await client_for(handler).fetch()
    assert error.value.message == message
    assert "secret-host" not in str(error.value)


async def test_app_starts_without_ms2_url_and_builds_client_with_it() -> None:
    from receipes.main import create_app

    plain = create_app(unavailable_db_settings())
    with_url = create_app(unavailable_db_settings(ms2_base_url="http://ms2.test:8000"))
    try:
        assert plain.state.dictionaries is None
        assert isinstance(with_url.state.dictionaries, DictionaryClient)
    finally:
        await with_url.state.dictionaries.aclose()
        for app in (plain, with_url):
            await app.state.engine.dispose()
            app.state.tracer_provider.shutdown()

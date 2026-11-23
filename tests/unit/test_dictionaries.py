import asyncio
import time

import httpx
import pytest
from fakes import unavailable_db_settings

from receipes.dictionaries import Dictionaries, DictionaryClient, find_unknown_ids
from receipes.domain.errors import DictionaryError

PAYLOAD = {
    "habits": [{"id": 1, "name": "Быстрые перекусы"}],
    "diets": [{"id": 4, "name": "Средиземноморская"}],
    "allergies": [{"id": 1, "name": "Глютен"}, {"id": 2, "name": "Лактоза"}],
    "kitchenTools": [{"id": 6, "name": "Блендер"}],
}


def client_for(handler) -> DictionaryClient:
    settings = unavailable_db_settings(ms2_base_url="http://ms2.test:8000")
    return DictionaryClient.from_settings(settings, httpx.MockTransport(handler))


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


async def test_app_always_builds_the_dictionary_client() -> None:
    from receipes.main import create_app

    app = create_app(unavailable_db_settings())
    try:
        assert isinstance(app.state.dictionaries, DictionaryClient)
    finally:
        await app.state.dictionaries.aclose()
        await app.state.engine.dispose()
        app.state.tracer_provider.shutdown()


@pytest.mark.parametrize(
    "broken",
    [
        {**PAYLOAD, "habits": []},
        {**PAYLOAD, "diets": []},
        {**PAYLOAD, "allergies": []},
        {**PAYLOAD, "kitchenTools": []},
        {**PAYLOAD, "allergies": [{"id": 1, "name": "a"}, {"id": 1, "name": "b"}]},
        # Пустое название дало бы в промпте строку вида «привычки: , B».
        {**PAYLOAD, "habits": [{"id": 1, "name": ""}]},
        {**PAYLOAD, "diets": [{"id": 1, "name": "   \n"}]},
    ],
)
async def test_broken_dictionary_seed_is_a_server_error(broken: dict) -> None:
    with pytest.raises(DictionaryError) as error:
        await client_for(lambda r: httpx.Response(200, json=broken)).fetch()
    assert error.value.message == "Invalid dictionaries response"


async def test_slow_dictionaries_hit_the_overall_deadline() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=PAYLOAD)

    settings = unavailable_db_settings(ms2_timeout_seconds=0.2)
    client = DictionaryClient.from_settings(settings, httpx.MockTransport(handler))
    started = time.monotonic()
    try:
        with pytest.raises(DictionaryError) as error:
            await client.fetch()
    finally:
        await client.aclose()
    assert error.value.message == "Dictionaries timeout"
    assert time.monotonic() - started < 1


DICTIONARIES = Dictionaries(
    habits={1: "a", 2: "b", 3: "c"},
    diets={4: "d"},
    allergies={1: "x"},
    kitchen_tools={6: "t"},
)


def unknown(**fields) -> list:
    values = {"habits": [], "diet": None, "allergies": [], "kitchen_tools": []}
    return find_unknown_ids(DICTIONARIES, **{**values, **fields})


def test_known_ids_have_no_errors() -> None:
    assert unknown(habits=[3, 1], diet=4, allergies=[1], kitchen_tools=[6]) == []
    assert unknown() == []


def test_unknown_id_points_to_the_element_in_the_request_order() -> None:
    assert unknown(habits=[3, 99, 1]) == [("body", "habits", 1)]


def test_repeated_unknown_id_is_reported_for_every_occurrence() -> None:
    assert unknown(habits=[99, 99]) == [("body", "habits", 0), ("body", "habits", 1)]


def test_every_field_is_checked_in_request_order() -> None:
    assert unknown(habits=[99], diet=99, allergies=[99], kitchen_tools=[1, 99]) == [
        ("body", "habits", 0),
        ("body", "diet"),
        ("body", "allergies", 0),
        ("body", "kitchenTools", 0),
        ("body", "kitchenTools", 1),
    ]


def test_id_from_another_dictionary_is_unknown() -> None:
    # 6 есть среди техники, но не среди привычек; 0 и отрицательные неизвестны.
    assert unknown(habits=[6, 0, -1]) == [
        ("body", "habits", 0),
        ("body", "habits", 1),
        ("body", "habits", 2),
    ]
    assert unknown(diet=0) == [("body", "diet")]


async def test_dictionary_names_are_stripped() -> None:
    payload = {**PAYLOAD, "habits": [{"id": 1, "name": "  Быстрые перекусы \n"}]}
    result = await client_for(lambda r: httpx.Response(200, json=payload)).fetch()
    assert result.habits == {1: "Быстрые перекусы"}

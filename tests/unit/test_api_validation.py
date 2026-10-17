import asyncio
import json
import time

import httpx
import pytest
from fakes import unavailable_db_settings

from receipes.api.dependencies import get_task_store
from receipes.main import create_app

PAYLOAD = {
    "habits": [{"id": i, "name": f"habit {i}"} for i in (1, 2, 3, 4, 5)],
    "diets": [{"id": 4, "name": "diet"}],
    "allergies": [{"id": 1, "name": "gluten"}, {"id": 2, "name": "lactose"}],
    "kitchenTools": [{"id": 1, "name": "oven"}, {"id": 6, "name": "blender"}],
}
BASE = {"peopleCount": 4, "weeklyBudget": 5000, "habits": [], "kitchenTools": []}
EMPTY = {**BASE}


class FakeStore:
    def __init__(self) -> None:
        self.params: list = []

    async def create(self, params):
        self.params.append(params)
        return "b1e2c3d4-5678-90ab-cdef-1234567890ab"


class Ms2:
    """Подмена сети MS-2: считает обращения и отдаёт заданный ответ."""

    def __init__(self, handler=None) -> None:
        self.calls = 0
        self.handler = handler or (lambda request: httpx.Response(200, json=PAYLOAD))

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        result = self.handler(request)
        if hasattr(result, "__await__"):
            result = await result
        return result


async def shutdown(app, client) -> None:
    await client.aclose()
    await app.state.dictionaries.aclose()
    await app.state.engine.dispose()
    app.state.tracer_provider.shutdown()


async def make_client(ms2: Ms2, store: FakeStore, **settings):
    app = create_app(
        unavailable_db_settings(**settings), ms2_transport=httpx.MockTransport(ms2)
    )
    app.dependency_overrides[get_task_store] = lambda: store
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )
    return app, client


@pytest.fixture
async def setup():
    ms2, store = Ms2(), FakeStore()
    app, client = await make_client(ms2, store)
    yield client, ms2, store
    await shutdown(app, client)


def response_error(response: httpx.Response) -> dict:
    return response.json()["error"]


def fields(response: httpx.Response) -> list[dict]:
    return response.json()["error"]["details"]["fields"]


async def test_empty_selection_is_accepted_without_calling_ms2(setup) -> None:
    client, ms2, store = setup
    response = await client.post("/generate-menu", json={**EMPTY, "diet": None})
    assert response.status_code == 202
    assert ms2.calls == 0
    assert len(store.params) == 1


async def test_empty_selection_is_accepted_when_ms2_is_down() -> None:
    def down(request):
        raise httpx.ConnectError("secret-host")

    ms2, store = Ms2(down), FakeStore()
    app, client = await make_client(ms2, store)
    try:
        response = await client.post("/generate-menu", json=EMPTY)
        assert response.status_code == 202
    finally:
        await shutdown(app, client)


async def test_explicit_empty_allergies_are_accepted_without_calling_ms2(setup) -> None:
    client, ms2, store = setup
    response = await client.post("/generate-menu", json={**EMPTY, "allergies": []})
    assert response.status_code == 202
    assert ms2.calls == 0


async def test_known_ids_are_accepted(setup) -> None:
    client, ms2, store = setup
    response = await client.post(
        "/generate-menu",
        json={
            **BASE,
            "habits": [3, 5, 1],
            "diet": 4,
            "allergies": [1, 2],
            "kitchenTools": [1, 6],
        },
    )
    assert response.status_code == 202
    assert ms2.calls == 1
    assert len(store.params) == 1


async def test_diet_zero_is_checked_too(setup) -> None:
    client, ms2, store = setup
    response = await client.post("/generate-menu", json={**BASE, "diet": 0})
    assert response.status_code == 400
    assert fields(response) == [{"loc": ["body", "diet"], "type": "unknown_id"}]


async def test_three_distinct_habits_pass_and_four_are_rejected(setup) -> None:
    client, ms2, store = setup
    ok = await client.post("/generate-menu", json={**BASE, "habits": [1, 2, 3]})
    assert ok.status_code == 202
    too_many = await client.post(
        "/generate-menu", json={**BASE, "habits": [1, 2, 3, 4]}
    )
    assert too_many.status_code == 400
    assert response_error(too_many)["code"] == "VALIDATION_ERROR"
    assert fields(too_many) == [{"loc": ["body", "habits"], "type": "too_long"}]


async def test_repeated_habits_count_once(setup) -> None:
    client, ms2, store = setup
    response = await client.post(
        "/generate-menu", json={**BASE, "habits": [1, 1, 1, 1]}
    )
    assert response.status_code == 202


async def test_too_many_habits_do_not_call_ms2(setup) -> None:
    client, ms2, store = setup
    response = await client.post(
        "/generate-menu", json={**BASE, "habits": [1, 2, 3, 4]}
    )
    assert response.status_code == 400
    assert ms2.calls == 0


async def test_unknown_id_points_to_the_request_element_not_the_sorted_one(
    setup,
) -> None:
    client, ms2, store = setup
    response = await client.post("/generate-menu", json={**BASE, "habits": [5, 99, 3]})
    assert response.status_code == 400
    assert fields(response) == [{"loc": ["body", "habits", "1"], "type": "unknown_id"}]
    assert store.params == []


async def test_repeated_unknown_id_is_reported_on_every_occurrence(setup) -> None:
    client, ms2, store = setup
    response = await client.post("/generate-menu", json={**BASE, "habits": [99, 99]})
    assert fields(response) == [
        {"loc": ["body", "habits", "0"], "type": "unknown_id"},
        {"loc": ["body", "habits", "1"], "type": "unknown_id"},
    ]


async def test_all_errors_come_in_one_response_without_values(setup) -> None:
    client, ms2, store = setup
    response = await client.post(
        "/generate-menu",
        json={
            **BASE,
            "habits": [123456],
            "diet": 654321,
            "allergies": [1, 777777],
            "kitchenTools": [888888],
        },
    )
    assert response.status_code == 400
    assert response_error(response)["code"] == "VALIDATION_ERROR"
    assert fields(response) == [
        {"loc": ["body", "habits", "0"], "type": "unknown_id"},
        {"loc": ["body", "diet"], "type": "unknown_id"},
        {"loc": ["body", "allergies", "1"], "type": "unknown_id"},
        {"loc": ["body", "kitchenTools", "0"], "type": "unknown_id"},
    ]
    for value in ("123456", "654321", "777777", "888888"):
        assert value not in response.text


async def test_id_from_another_dictionary_is_rejected(setup) -> None:
    client, ms2, store = setup
    # 6 есть среди техники, но не среди привычек.
    response = await client.post("/generate-menu", json={**BASE, "habits": [6]})
    assert fields(response) == [{"loc": ["body", "habits", "0"], "type": "unknown_id"}]


@pytest.mark.parametrize(
    "body",
    [
        {**BASE, "habits": ["1"]},
        {**BASE, "habits": [True]},
        {**BASE, "diet": "4"},
        {**BASE, "kitchenTools": [2**31]},
        {**BASE, "peopleCount": 0, "habits": [1]},
    ],
)
async def test_invalid_schema_never_reaches_ms2(setup, body) -> None:
    client, ms2, store = setup
    response = await client.post("/generate-menu", json=body)
    assert response.status_code == 400
    assert ms2.calls == 0


def _raise_connect(request):
    raise httpx.ConnectError("secret-host")


def _raise_timeout(request):
    raise httpx.ReadTimeout("secret-host")


BROKEN_MS2 = [
    pytest.param(_raise_connect, id="unreachable"),
    pytest.param(_raise_timeout, id="timeout"),
    pytest.param(lambda r: httpx.Response(500), id="http-500"),
    pytest.param(lambda r: httpx.Response(404), id="http-404"),
    pytest.param(lambda r: httpx.Response(200, text="not json"), id="not-json"),
    pytest.param(lambda r: httpx.Response(200, json={"habits": []}), id="no-keys"),
    pytest.param(
        lambda r: httpx.Response(
            200, json={**PAYLOAD, "habits": [{"id": "1", "name": "x"}]}
        ),
        id="id-as-string",
    ),
    pytest.param(
        lambda r: httpx.Response(200, json={**PAYLOAD, "diets": []}),
        id="empty-dictionary",
    ),
    pytest.param(
        lambda r: httpx.Response(
            200,
            json={**PAYLOAD, "diets": [{"id": 4, "name": "a"}, {"id": 4, "name": "b"}]},
        ),
        id="duplicate-id",
    ),
]


@pytest.mark.parametrize("handler", BROKEN_MS2)
async def test_broken_ms2_gives_500_and_no_task(handler, capsys) -> None:
    ms2, store = Ms2(handler), FakeStore()
    app, client = await make_client(ms2, store)
    try:
        response = await client.post(
            "/generate-menu", json={**BASE, "habits": [424242], "diet": 4}
        )
    finally:
        await shutdown(app, client)
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
            "details": {},
        }
    }
    assert response.headers["x-request-id"]
    assert store.params == []
    output = capsys.readouterr()
    logged = output.out + output.err
    assert "dictionaries_failed" in logged
    assert "secret-host" not in response.text + logged
    # Адрес MS-2 не попадает ни в ответ, ни в наше событие лога (httpx сам пишет
    # свою строку «HTTP Request: GET ...», это общее для всех клиентов сервиса).
    event = "".join(
        line for line in logged.splitlines() if "dictionaries_failed" in line
    )
    assert event
    assert "ms2.test" not in response.text + event
    assert "424242" not in response.text + logged


async def test_hanging_ms2_ends_within_the_timeout() -> None:
    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json=PAYLOAD)

    ms2, store = Ms2(hang), FakeStore()
    app, client = await make_client(ms2, store, ms2_timeout_seconds=0.2)
    started = time.monotonic()
    try:
        response = await client.post("/generate-menu", json={**BASE, "habits": [1]})
    finally:
        await shutdown(app, client)
    assert response.status_code == 500
    assert time.monotonic() - started < 1
    assert store.params == []


def test_openapi_documents_the_new_checks() -> None:
    app = create_app(unavailable_db_settings())
    schema = app.openapi()
    post = schema["paths"]["/generate-menu"]["post"]
    text = json.dumps(post, ensure_ascii=False)
    assert "unknown_id" in text
    assert "too_long" in text
    examples = post["responses"]["400"]["content"]["application/json"]["examples"]
    assert examples["unknown_id"]["value"]["error"]["details"]["fields"][0] == {
        "loc": ["body", "habits", "1"],
        "type": "unknown_id",
    }
    assert examples["habits_too_many"]["value"]["error"]["details"]["fields"] == [
        {"loc": ["body", "habits"], "type": "too_long"}
    ]
    request = schema["components"]["schemas"]["GenerateMenuRequest"]["properties"]
    assert "3" in request["habits"]["description"]
    for name in ("habits", "diet", "allergies", "kitchenTools"):
        assert "unknown_id" in request[name]["description"]
    assert (
        "unknown_id"
        in schema["components"]["schemas"]["ErrorBody"]["properties"]["details"][
            "description"
        ]
    )

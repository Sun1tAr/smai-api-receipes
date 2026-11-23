from decimal import Decimal

import httpx
import pytest
from fakes import unavailable_db_settings
from test_api_menu import FakeDictionaries, FakeStore

from receipes.api.dependencies import get_dictionaries, get_task_store, parse_user_id
from receipes.db.models import (
    GenerationParameterAllergen,
    GenerationParameterHabit,
    GenerationParameterKitchenTool,
    GenerationParameters,
)

BODY = {"peopleCount": 4, "weeklyBudget": 5000, "habits": [], "kitchenTools": []}


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
async def client(store: FakeStore):
    from receipes.main import create_app

    app = create_app(unavailable_db_settings())
    app.dependency_overrides[get_task_store] = lambda: store
    app.dependency_overrides[get_dictionaries] = FakeDictionaries
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield client
    await app.state.engine.dispose()
    app.state.tracer_provider.shutdown()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1),
        ("42", 42),
        ("2147483647", 2147483647),
        (None, None),
        ("0", None),
        ("-1", None),
        ("+1", None),
        ("1.0", None),
        (" 1", None),
        ("abc", None),
        ("", None),
        ("2147483648", None),
        ("99999999999999999999", None),
        ("١٢", None),  # арабо-индийские цифры
    ],
)
def test_parse_user_id(raw: str | None, expected: int | None) -> None:
    assert parse_user_id([] if raw is None else [raw]) == expected


async def test_guest_request_has_no_owner(client, store) -> None:
    response = await client.post("/generate-menu", json=BODY)
    assert response.status_code == 202
    assert store.user_ids == [None]


async def test_owner_is_passed_to_the_store(client, store) -> None:
    response = await client.post(
        "/generate-menu", json=BODY, headers={"X-User-Id": "42"}
    )
    assert response.status_code == 202
    assert store.user_ids == [42]


@pytest.mark.parametrize(
    "value", ["0", "-5", "abc", "", "1.5", "2147483648", "99999999999999999999"]
)
async def test_invalid_owner_is_a_validation_error(client, store, value) -> None:
    response = await client.post(
        "/generate-menu", json=BODY, headers={"X-User-Id": value}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {
        "fields": [{"loc": ["header", "X-User-Id"], "type": "user_id"}]
    }
    assert value not in response.text or value == ""
    assert store.user_ids == []


async def test_duplicate_owner_header_is_rejected(client, store) -> None:
    response = await client.post(
        "/generate-menu",
        json=BODY,
        headers=[("X-User-Id", "7"), ("X-User-Id", "7")],
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"] == {
        "fields": [{"loc": ["header", "X-User-Id"], "type": "user_id"}]
    }
    assert store.user_ids == []


async def test_duplicate_owner_header_on_profile_is_401(client) -> None:
    response = await client.get(
        "/profile/parameters", headers=[("X-User-Id", "7"), ("x-user-id", "8")]
    )
    assert response.status_code == 401


async def test_profile_requires_a_valid_user(client) -> None:
    for headers in ({}, {"X-User-Id": "0"}, {"X-User-Id": "x"}):
        response = await client.get("/profile/parameters", headers=headers)
        assert response.status_code == 401
        assert response.json() == {
            "error": {
                "code": "UNAUTHORIZED",
                "message": "User is not identified",
                "details": {},
            }
        }


async def test_profile_without_saved_parameters_is_an_empty_200(client) -> None:
    response = await client.get("/profile/parameters", headers={"X-User-Id": "5"})
    assert response.status_code == 200
    assert response.json() == {
        "habits": [],
        "diet": None,
        "allergies": [],
        "kitchenTools": [],
    }


async def test_profile_returns_sorted_camel_case_parameters(client, store) -> None:
    row = GenerationParameters(
        people_count=4,
        weekly_budget=Decimal("5000"),
        diet_id=None,
        habits=[
            GenerationParameterHabit(habit_id=5),
            GenerationParameterHabit(habit_id=3),
        ],
        allergies=[GenerationParameterAllergen(allergen_id=2)],
        kitchen_tools=[
            GenerationParameterKitchenTool(kitchen_tool_id=6),
            GenerationParameterKitchenTool(kitchen_tool_id=1),
        ],
    )
    store.saved[5] = row
    response = await client.get("/profile/parameters", headers={"X-User-Id": "5"})
    assert response.json() == {
        "habits": [3, 5],
        "diet": None,
        "allergies": [2],
        "kitchenTools": [1, 6],
    }
    row.diet_id = 4
    other = await client.get("/profile/parameters", headers={"X-User-Id": "6"})
    assert other.json()["diet"] is None
    mine = await client.get("/profile/parameters", headers={"X-User-Id": "5"})
    assert mine.json()["diet"] == 4
    assert "peopleCount" not in mine.text and "weeklyBudget" not in mine.text


async def test_profile_store_failure_is_internal_error(client, store) -> None:
    store.fail = True
    response = await client.get("/profile/parameters", headers={"X-User-Id": "5"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in response.text

import json
from typing import Any

import httpx
import jsonschema
import pytest

from receipes.domain import capitalize_ingredient_name
from tests.unit.fakes import unavailable_db_settings


@pytest.fixture
async def app():
    from receipes.main import create_app

    app = create_app(unavailable_db_settings())
    yield app
    await app.state.engine.dispose()
    app.state.tracer_provider.shutdown()


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield client


async def post(client, body: Any = None, **kwargs: Any) -> httpx.Response:
    if body is not None:
        kwargs["content"] = body if isinstance(body, str) else json.dumps(body)
    return await client.post(
        "/generate-menu", headers={"Content-Type": "application/json"}, **kwargs
    )


def fields(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "Request validation failed"
    return error["details"]["fields"]


@pytest.mark.parametrize(
    ("people", "type_"),
    [
        (0, "greater_than_equal"),
        (21, "less_than_equal"),
        (-1, "greater_than_equal"),
        (2.5, "int_type"),
        (2.0, "int_type"),
        ("2", "int_type"),
        (True, "int_type"),
        (None, "int_type"),
    ],
)
async def test_invalid_people_count_is_a_400(client, people, type_) -> None:
    response = await post(client, {"peopleCount": people, "weeklyBudget": 5000})
    assert fields(response) == [{"loc": ["body", "peopleCount"], "type": type_}]


@pytest.mark.parametrize(
    ("budget", "type_"),
    [
        (0, "greater_than"),
        (-5, "greater_than"),
        (1000000.01, "less_than_equal"),
        (1000001, "less_than_equal"),
        (10.001, "decimal_max_places"),
        ("5000", "number_type"),
        (True, "number_type"),
        (None, "number_type"),
        ([], "number_type"),
    ],
)
async def test_invalid_weekly_budget_is_a_400(client, budget, type_) -> None:
    response = await post(client, {"peopleCount": 2, "weeklyBudget": budget})
    assert fields(response) == [{"loc": ["body", "weeklyBudget"], "type": type_}]


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_budget_is_a_400(client, literal) -> None:
    response = await post(client, f'{{"peopleCount": 2, "weeklyBudget": {literal}}}')
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_missing_extra_and_empty_bodies_are_a_400(client) -> None:
    missing = await post(client, {"peopleCount": 2})
    assert fields(missing) == [{"loc": ["body", "weeklyBudget"], "type": "missing"}]
    extra = await post(
        client, {"peopleCount": 2, "weeklyBudget": 10, "dietType": "vegan"}
    )
    assert fields(extra) == [{"loc": ["body", "dietType"], "type": "extra_forbidden"}]
    snake = await post(client, {"people_count": 2, "weeklyBudget": 10})
    assert {(tuple(f["loc"]), f["type"]) for f in fields(snake)} == {
        (("body", "peopleCount"), "missing"),
        (("body", "people_count"), "extra_forbidden"),
    }
    empty = await post(client)
    assert fields(empty)[0]["loc"] == ["body"]
    both = await post(client, {})
    assert len(fields(both)) == 2


async def test_malformed_json_and_wrong_shape_are_a_400(client) -> None:
    broken = await post(client, "{bad")
    assert fields(broken) == [{"loc": ["body"], "type": "json_invalid"}]
    for body in ("[]", '"text"', "null", "42"):
        assert (await post(client, body)).status_code == 400


async def test_validation_errors_do_not_echo_input(client) -> None:
    response = await post(client, {"peopleCount": "secret-value", "weeklyBudget": 1})
    assert "secret-value" not in response.text


@pytest.mark.parametrize(
    "task_id", ["not-a-uuid", "123", "00000000-0000-0000-0000-00000000000g", "%20"]
)
async def test_garbage_task_id_is_a_404_not_a_validation_error(client, task_id) -> None:
    response = await client.get(f"/generate-menu/{task_id}")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Generation task not found",
            "details": {},
        }
    }
    assert "X-Request-ID" in response.headers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("овсяные хлопья", "Овсяные хлопья"),
        ("ОВСЯНЫЕ ХЛОПЬЯ", "ОВСЯНЫЕ ХЛОПЬЯ"),
        ("Мёд", "Мёд"),
        ("мёд", "Мёд"),
        ("яйцо", "Яйцо"),
        ("a", "A"),
        ("", ""),
        ("1% молоко", "1% молоко"),
        ("ßeta", "Sseta"),
        ("ǆ", "ǅ"),
        ("ǆudo", "ǅudo"),
        ("иван-чай", "Иван-чай"),
        ("Иван-чай", "Иван-чай"),
        ("ёжик", "Ёжик"),
        ("  пробел", "  пробел"),
    ],
)
def test_ingredient_name_capitalization(raw: str, expected: str) -> None:
    assert capitalize_ingredient_name(raw) == expected


async def test_openapi_documents_both_endpoints_completely(client) -> None:
    document = (await client.get("/openapi.json")).json()
    assert set(document["paths"]) == {
        "/generate-menu",
        "/generate-menu/{task_id}",
        "/health",
    }
    post_op = document["paths"]["/generate-menu"]["post"]
    get_op = document["paths"]["/generate-menu/{task_id}"]["get"]
    for operation in (post_op, get_op):
        assert operation["summary"] and operation["description"]
        assert operation["tags"] == ["generate-menu"]
    assert set(post_op["responses"]) == {"202", "400", "500"}
    assert set(get_op["responses"]) == {"200", "404", "500"}
    assert "422" not in json.dumps(document)
    assert "HTTPValidationError" not in json.dumps(document)
    for operation in (post_op, get_op):
        for code, response in operation["responses"].items():
            if code in {"400", "404", "500"}:
                content = response["content"]["application/json"]
                assert content["schema"]["$ref"].endswith("/ErrorResponse")
                assert content["example"]["error"]["code"] in {
                    "VALIDATION_ERROR",
                    "NOT_FOUND",
                    "INTERNAL_ERROR",
                }
    examples = get_op["responses"]["200"]["content"]["application/json"]["examples"]
    assert set(examples) == {"pending", "in_progress", "done", "failed"}
    assert examples["done"]["value"]["menu"]["recipes"]
    assert examples["failed"]["value"]["error"]
    assert examples["pending"]["value"]["menu"] is None


async def test_openapi_request_schema_and_examples_are_consistent(client) -> None:
    document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    request_schema = schemas["GenerateMenuRequest"]
    assert request_schema["required"] == ["peopleCount", "weeklyBudget"]
    assert request_schema["additionalProperties"] is False
    properties = request_schema["properties"]
    assert (
        properties["peopleCount"]["minimum"],
        properties["peopleCount"]["maximum"],
    ) == (1, 20)
    assert properties["weeklyBudget"]["exclusiveMinimum"] == 0
    assert properties["weeklyBudget"]["maximum"] == 1000000
    jsonschema.validate(request_schema["example"], request_schema)
    assert schemas["TaskCreatedResponse"]["example"]["task_id"]
    status_schema = schemas["TaskStatusResponse"]
    assert set(status_schema["required"]) == {"status", "menu", "error"}
    assert schemas["TaskStatus"]["enum"] == ["pending", "in_progress", "done", "failed"]
    recipe = schemas["RecipeResponse"]["properties"]
    assert "cookingTime" in recipe and "photo" in recipe


async def test_swagger_ui_is_served(client) -> None:
    assert (await client.get("/docs")).status_code == 200


async def test_giant_unknown_field_name_does_not_inflate_the_response(client) -> None:
    key = "k" * 5_000_000
    body = '{"peopleCount": 2, "weeklyBudget": 10, "' + key + '": 1}'
    response = await post(client, body)
    assert response.status_code == 400
    assert len(response.content) < 1000
    loc = fields(response)[0]["loc"]
    assert loc[0] == "body" and loc[1] == "k" * 64 + "…"
    assert key not in response.text


async def test_short_unknown_field_name_is_reported_as_is(client) -> None:
    response = await post(client, {"peopleCount": 2, "weeklyBudget": 10, "x" * 64: 1})
    assert fields(response) == [{"loc": ["body", "x" * 64], "type": "extra_forbidden"}]


@pytest.mark.parametrize("depth", [1000, 50_000])
async def test_deeply_nested_json_is_a_validation_error(client, depth) -> None:
    response = await post(client, "[" * depth)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fields(response) == [{"loc": ["body"], "type": "json_invalid"}]
    nested = await post(client, '{"peopleCount": ' + "[" * depth + "]" * depth + "}")
    assert nested.status_code == 400
    assert nested.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_openapi_400_example_matches_a_live_response(client) -> None:
    document = (await client.get("/openapi.json")).json()
    example = document["paths"]["/generate-menu"]["post"]["responses"]["400"][
        "content"
    ]["application/json"]["example"]
    live = await post(client, {"peopleCount": "4", "weeklyBudget": 5000})
    assert live.json() == example
    assert example["error"]["code"] == "VALIDATION_ERROR"


async def test_openapi_202_response_has_an_example(client) -> None:
    document = (await client.get("/openapi.json")).json()
    accepted = document["paths"]["/generate-menu"]["post"]["responses"]["202"]
    content = accepted["content"]["application/json"]
    assert content["example"]["task_id"]
    assert content["schema"]["$ref"].endswith("/TaskCreatedResponse")

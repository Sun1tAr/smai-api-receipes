"""Контрактные тесты MS-4.

1. Совместимость с контрактом gateway (tests/contract/gateway.receipes.openapi.json,
   копия contracts/receipes.openapi.json репозитория smai-api-gateway): пути, коды
   ответов, схема запроса, примеры в обе стороны.
2. Снимок собственной OpenAPI (tests/contract/openapi.snapshot.json): тест падает при
   любом незамеченном изменении схемы. Осознанное изменение фиксируется командой
   `UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/test_contract.py`.
"""

import copy
import json
import os
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest

from receipes.errors import ErrorResponse
from receipes.schemas import (
    GenerateMenuRequest,
    TaskCreatedResponse,
    TaskStatusResponse,
)
from tests.unit.fakes import unavailable_db_settings

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contract"
GATEWAY_CONTRACT = CONTRACT_DIR / "gateway.receipes.openapi.json"
SNAPSHOT = CONTRACT_DIR / "openapi.snapshot.json"


def gateway() -> dict[str, Any]:
    contract: dict[str, Any] = json.loads(GATEWAY_CONTRACT.read_text())
    return contract


def operations(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (path, method): operation
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
    }


def json_content(response: dict[str, Any]) -> dict[str, Any]:
    content: dict[str, Any] = response["content"]["application/json"]
    return content


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


@pytest.fixture
async def ours(client) -> dict[str, Any]:
    document: dict[str, Any] = (await client.get("/openapi.json")).json()
    return document


# --- совместимость с контрактом gateway -----------------------------------------


async def test_every_gateway_operation_and_status_code_is_implemented(ours) -> None:
    ours_ops = operations(ours)
    for key, operation in operations(gateway()).items():
        assert key in ours_ops, f"missing operation {key}"
        missing = set(operation["responses"]) - set(ours_ops[key]["responses"])
        assert not missing, f"{key} lacks response codes {missing}"


async def test_request_schema_matches_the_gateway_contract(ours) -> None:
    expected = json_content(
        gateway()["paths"]["/generate-menu"]["post"]["requestBody"]
    )["schema"]
    actual = ours["components"]["schemas"]["GenerateMenuRequest"]
    assert set(actual["required"]) == set(expected["required"])
    assert set(actual["properties"]) == set(expected["properties"])
    for name, schema in expected["properties"].items():
        for bound in ("minimum", "maximum", "exclusiveMinimum"):
            assert actual["properties"][name].get(bound) == schema.get(bound), (
                name,
                bound,
            )
    example = json_content(gateway()["paths"]["/generate-menu"]["post"]["requestBody"])[
        "example"
    ]
    GenerateMenuRequest.model_validate(example)


def test_gateway_status_examples_round_trip_through_our_models() -> None:
    operation = gateway()["paths"]["/generate-menu/{task_id}"]["get"]
    examples = json_content(operation["responses"]["200"])["examples"]
    assert set(examples) == {"pending", "in_progress", "done", "failed"}
    for name, example in examples.items():
        value = example["value"]
        parsed = TaskStatusResponse.model_validate(value)
        dumped = parsed.model_dump(mode="json", by_alias=True)
        assert dumped == value, name


def test_our_status_responses_satisfy_the_gateway_schema() -> None:
    operation = gateway()["paths"]["/generate-menu/{task_id}"]["get"]
    schema = copy.deepcopy(json_content(operation["responses"]["200"])["schema"])
    # В контракте gateway пример done содержит один рецепт, хотя схема требует не
    # менее трёх (minItems: 3): пример противоречит собственной схеме. Для сверки
    # примера минимум снимается; сам минимум проверяют тесты «ворот» GeneratedMenu.
    del schema["properties"]["menu"]["properties"]["recipes"]["minItems"]
    for example in json_content(operation["responses"]["200"])["examples"].values():
        body = TaskStatusResponse.model_validate(example["value"]).model_dump(
            mode="json", by_alias=True
        )
        jsonschema.validate(body, schema)


def test_accepted_response_matches_the_gateway_example() -> None:
    operation = gateway()["paths"]["/generate-menu"]["post"]
    example = json_content(operation["responses"]["202"])["example"]
    assert TaskCreatedResponse.model_validate(example).model_dump() == example
    jsonschema.validate(example, json_content(operation["responses"]["202"])["schema"])


def test_gateway_error_examples_are_valid_in_our_error_format() -> None:
    for operation in gateway()["paths"].values():
        for method in operation.values():
            for code, response in method["responses"].items():
                if code in {"400", "404", "500"}:
                    example = json_content(response)["example"]
                    ErrorResponse.model_validate(example)
                    jsonschema.validate(example, json_content(response)["schema"])


async def test_real_error_bodies_satisfy_the_gateway_schemas(client) -> None:
    contract = gateway()["paths"]
    bad = await client.post(
        "/generate-menu", json={"peopleCount": 0, "weeklyBudget": 1}
    )
    schema_400 = json_content(contract["/generate-menu"]["post"]["responses"]["400"])
    assert bad.status_code == 400
    jsonschema.validate(bad.json(), schema_400["schema"])
    assert bad.json()["error"]["code"] == schema_400["example"]["error"]["code"]
    missing = await client.get("/generate-menu/not-a-uuid")
    schema_404 = json_content(
        contract["/generate-menu/{task_id}"]["get"]["responses"]["404"]
    )
    assert missing.status_code == 404
    jsonschema.validate(missing.json(), schema_404["schema"])
    assert missing.json() == schema_404["example"]


async def test_error_format_is_identical_for_every_error_status(client, app) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret")

    responses = {
        400: await client.post("/generate-menu", content="{bad"),
        404: await client.get("/generate-menu/00000000-0000-0000-0000-000000000000x"),
        405: await client.put("/generate-menu"),
        500: await client.get("/boom"),
        503: await client.get("/health"),
    }
    for status, response in responses.items():
        assert response.status_code == status
        body = response.json()
        assert set(body) == {"error"} and set(body["error"]) == {
            "code",
            "message",
            "details",
        }
        assert response.headers["X-Request-ID"]
        assert "secret" not in response.text
        ErrorResponse.model_validate(body)


# --- снимок собственной OpenAPI ---------------------------------------------------


async def test_openapi_matches_the_committed_snapshot(ours) -> None:
    rendered = json.dumps(ours, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if os.getenv("UPDATE_OPENAPI_SNAPSHOT"):
        SNAPSHOT.write_text(rendered)
    assert SNAPSHOT.exists(), "run with UPDATE_OPENAPI_SNAPSHOT=1 to create it"
    assert rendered == SNAPSHOT.read_text(), (
        "the OpenAPI schema changed: review the diff and refresh the snapshot with "
        "UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/test_contract.py"
    )

import json

import httpx
import jsonschema
import pytest
from fakes import unavailable_db_settings


@pytest.fixture
async def app():
    from receipes.main import create_app

    app = create_app(unavailable_db_settings())
    yield app
    await app.state.engine.dispose()
    app.state.tracer_provider.shutdown()


async def test_docs_and_schema_are_served(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")
    assert docs.status_code == 200
    assert schema.status_code == 200
    document = schema.json()
    assert document["info"]["title"] == "SmartMeal recipes service"
    assert document["info"]["version"] == "0.1.0"
    assert document["info"]["description"]


async def test_health_is_fully_documented_and_metrics_is_hidden(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        document = (await client.get("/openapi.json")).json()
    assert set(document["paths"]) == {
        "/health",
        "/generate-menu",
        "/generate-menu/{task_id}",
        "/profile/parameters",
    }
    operation = document["paths"]["/health"]["get"]
    assert operation["summary"]
    assert operation["description"]
    assert set(operation["responses"]) == {"200", "503"}
    ok = operation["responses"]["200"]["content"]["application/json"]
    error = operation["responses"]["503"]["content"]["application/json"]
    assert error["example"]["error"]["code"] == "INTERNAL_ERROR"
    schemas = document["components"]["schemas"]
    health_schema = schemas["HealthResponse"]
    assert health_schema["example"] == {"status": "ok"}
    assert ok["schema"]["$ref"].endswith("/HealthResponse")
    assert error["schema"]["$ref"].endswith("/ErrorResponse")
    # Примеры должны соответствовать собственным схемам.
    jsonschema.validate(health_schema["example"], health_schema)
    error_schema = schemas["ErrorResponse"]
    jsonschema.validate(
        error["example"], {**error_schema, "components": {"schemas": schemas}}
    )


# Для каждого эндпоинта: summary, description, коды ответов и примеры по схемам.
EXPECTED_CODES = {
    ("/health", "get"): {"200", "503"},
    ("/generate-menu", "post"): {"202", "400", "500"},
    ("/generate-menu/{task_id}", "get"): {"200", "404", "500"},
    ("/profile/parameters", "get"): {"200", "401", "500"},
}


async def test_every_operation_is_documented_with_exact_error_codes(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    assert "422" not in json.dumps(document)
    operations = {
        (path, method): operation
        for path, item in document["paths"].items()
        for method, operation in item.items()
    }
    assert set(operations) == set(EXPECTED_CODES)
    for key, operation in operations.items():
        assert operation["summary"] and operation["description"], key
        assert set(operation["responses"]) == EXPECTED_CODES[key], key
        for code, response in operation["responses"].items():
            assert response["description"], (key, code)
            if code.startswith(("4", "5")):
                content = response["content"]["application/json"]
                assert content["schema"]["$ref"].endswith("/ErrorResponse")
                examples = (
                    [content["example"]]
                    if "example" in content
                    else [item["value"] for item in content["examples"].values()]
                )
                for example in examples:
                    jsonschema.validate(
                        example,
                        {
                            **schemas["ErrorResponse"],
                            "components": {"schemas": schemas},
                        },
                    )


async def test_request_example_and_fields_are_documented(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        document = (await client.get("/openapi.json")).json()
    schema = document["components"]["schemas"]["GenerateMenuRequest"]
    jsonschema.validate(schema["example"], schema)
    assert set(schema["properties"]) == {
        "peopleCount",
        "weeklyBudget",
        "habits",
        "diet",
        "allergies",
        "kitchenTools",
    }
    assert set(schema["required"]) == {
        "peopleCount",
        "weeklyBudget",
        "habits",
        "kitchenTools",
    }
    assert all(item["description"] for item in schema["properties"].values())
    operation = document["paths"]["/generate-menu"]["post"]
    body_examples = operation["requestBody"]["content"]["application/json"]["examples"]
    assert {"full", "empty"} <= set(body_examples)
    for item in body_examples.values():
        jsonschema.validate(item["value"], schema)
    assert body_examples["empty"]["value"]["habits"] == []
    assert body_examples["empty"]["value"]["kitchenTools"] == []
    assert "habits" in operation["description"]
    assert "игнорируются" not in operation["description"]
    examples = operation["responses"]["400"]["content"]["application/json"]["examples"]
    assert {"habits_missing", "habits_item", "diet_type", "out_of_range"} <= set(
        examples
    )
    result = document["paths"]["/generate-menu/{task_id}"]["get"]["description"]
    assert "10 минут" in result and "порядок" in result
    error = document["components"]["schemas"]["TaskResultResponse"]["properties"][
        "error"
    ]["description"]
    for text in (
        "Neurocore unavailable",
        "Neurocore timeout",
        "Neurocore model not found",
        "Neurocore error",
        "Invalid neurocore response",
        "Neurocore response truncated",
        "Service restarted",
        "Menu generation failed",
    ):
        assert text in error


async def test_generate_menu_description_says_names_go_into_the_prompt(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        schema = (await client.get("/openapi.json")).json()
    description = schema["paths"]["/generate-menu"]["post"]["description"]
    assert "попадают в промпт" in description
    assert "пока не" not in description


async def test_profile_parameters_and_user_header_are_documented(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    operation = document["paths"]["/profile/parameters"]["get"]
    examples = operation["responses"]["200"]["content"]["application/json"]["examples"]
    assert set(examples) == {"saved", "empty"}
    assert examples["empty"]["value"]["diet"] is None
    for item in examples.values():
        jsonschema.validate(
            item["value"],
            {**schemas["SavedParametersResponse"], "components": {"schemas": schemas}},
        )
    for item in (
        operation,
        document["paths"]["/generate-menu"]["post"],
    ):
        header = next(p for p in item["parameters"] if p["in"] == "header")
        assert header["name"] == "X-User-Id"
        assert header["required"] is False
        assert header["description"]
    post = document["paths"]["/generate-menu"]["post"]
    assert (
        "user_id" in post["responses"]["400"]["content"]["application/json"]["examples"]
    )

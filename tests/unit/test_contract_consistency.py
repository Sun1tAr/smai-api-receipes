import copy
from uuid import UUID

import httpx
import jsonschema
from fakes import unavailable_db_settings
from menu_samples import ollama_reply, sample_menu

from receipes.domain.menu import parse_menu
from receipes.generation.ollama import RESPONSE_FORMAT


# Общие поля API-2 и API-3 совпадают; recipeId добавляет только backend.
async def test_openapi_done_example_is_a_valid_model_answer() -> None:
    from receipes.main import create_app

    app = create_app(unavailable_db_settings())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            document = (await client.get("/openapi.json")).json()
    finally:
        await app.state.engine.dispose()
        app.state.tracer_provider.shutdown()
    get = document["paths"]["/generate-menu/{task_id}"]["get"]
    examples = get["responses"]["200"]["content"]["application/json"]["examples"]
    value = examples["done"]["value"]
    schemas = document["components"]["schemas"]
    jsonschema.validate(
        value,
        {**schemas["TaskResultResponse"], "components": {"schemas": schemas}},
        format_checker=jsonschema.FormatChecker(),
    )
    ids = [UUID(recipe["recipeId"]) for recipe in value["menu"]["recipes"]]
    assert len(set(ids)) == len(ids)
    menu = copy.deepcopy(value["menu"])
    for recipe in menu["recipes"]:
        del recipe["recipeId"]
    jsonschema.validate(menu, RESPONSE_FORMAT)
    assert len(parse_menu(menu).recipes) == len(menu["recipes"])


def test_mock_fixtures_satisfy_format_and_gates() -> None:
    menu = sample_menu()
    jsonschema.validate(menu, RESPONSE_FORMAT)
    assert len(parse_menu(menu).recipes) == 3
    reply = ollama_reply(menu)
    assert reply["done"] is True and reply["done_reason"] == "stop"

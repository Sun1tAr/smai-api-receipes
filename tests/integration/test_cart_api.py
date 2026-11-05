import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import AnyHttpUrl
from sqlalchemy import select

from receipes.db.models import GenerationTask, MenuRecipe
from receipes.db.repositories import MenuRepository, TaskRepository
from receipes.domain.menu import MenuPayload
from receipes.domain.params import GenerationParams
from receipes.domain.status import TaskStatus
from receipes.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]


async def test_cart_uses_saved_recipe_origin_and_done_status(settings, session_factory):
    recipe_ids = {}
    async with session_factory() as session, session.begin():
        tasks = TaskRepository(session)
        menus = MenuRepository(session)
        for user_id, status in (
            (22, TaskStatus.DONE),
            (None, TaskStatus.DONE),
            (22, TaskStatus.FAILED),
        ):
            task_id = await tasks.create(
                GenerationParams(1, Decimal(1000)), user_id=user_id
            )
            menu_id = await menus.save(
                MenuPayload.model_validate(
                    {
                        "recipes": [
                            {
                                "name": f"Морковь {i}",
                                "ingredients": [
                                    {"name": "Морковь", "quantity": 10, "unit": "г"}
                                ],
                                "calories": 4,
                                "nutrition": {
                                    "calories": 4,
                                    "proteins": 1,
                                    "fats": 1,
                                    "carbohydrates": 1,
                                },
                                "cookingTime": 10,
                                "instructions": ["Нарезать"],
                            }
                            for i in range(3)
                        ]
                    }
                )
            )
            task = await session.get(GenerationTask, task_id)
            task.menu_id = menu_id
            task.status = status
            recipe_ids[user_id, status] = await session.scalar(
                select(MenuRecipe.recipe_id)
                .where(MenuRecipe.menu_id == menu_id)
                .limit(1)
            )

    capture = Path(__file__).parents[1] / "fixtures" / "mcp" / "init.json"
    initialize_result = json.loads(capture.read_text())["result"]
    calls = []

    def catalog(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        calls.append(method)
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = initialize_result
        else:
            assert method == "tools/call"
            tool = payload["params"]["name"]
            arguments = payload["params"]["arguments"]
            if tool == "vkusvill_product_details":
                assert arguments == {"id": 606}
                data = {
                    "id": 606,
                    "name": "Морковь",
                    "price": {"current": 53, "currency": "RUB"},
                    "unit": "кг",
                    "weight": None,
                }
            else:
                assert tool == "vkusvill_products_search"
                if "category_id" in arguments:
                    assert arguments["category_id"] == 287
                    data = {"items": [{"id": 606, "name": "Морковь", "unit": "кг"}]}
                else:
                    data = {
                        "items": [],
                        "facets": {
                            "categories": [
                                {
                                    "category_id": 287,
                                    "category_name": "Овощи, фрукты, ягоды, зелень",
                                    "subcategory_name": "",
                                }
                            ]
                        },
                    }
            result = {
                "isError": False,
                "content": [
                    {"type": "text", "text": json.dumps({"ok": True, "data": data})}
                ],
            }
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )

    settings = settings.model_copy(
        update={"vkusvill_mcp_url": AnyHttpUrl("http://catalog.test/mcp")}
    )
    app = create_app(settings, mcp_transport=httpx.MockTransport(catalog))
    async with app.router.lifespan_context(app):
        assert calls == []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for headers in ({}, {"X-User-Id": "99"}):
                response = await client.post(
                    "/recipes/cart",
                    json={"recipeId": str(recipe_ids[22, TaskStatus.DONE])},
                    headers=headers,
                )
                assert response.status_code == 200
                result = response.json()
                assert result["total"] == 0.53 and result["allMatched"] is True
                assert result["matches"][0]["ingredient"] == {
                    "name": "Морковь",
                    "quantity": 10,
                    "unit": "г",
                }

            for recipe_id in (
                recipe_ids[None, TaskStatus.DONE],
                recipe_ids[22, TaskStatus.FAILED],
                uuid4(),
            ):
                response = await client.post(
                    "/recipes/cart", json={"recipeId": str(recipe_id)}
                )
                assert response.status_code == 404
                assert response.json()["error"]["code"] == "NOT_FOUND"
    assert calls == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
        "tools/call",
    ]

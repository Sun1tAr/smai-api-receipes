import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from menu_samples import ollama_reply, sample_menu, sample_recipe
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_generation_parameters import Ms2Stub
from test_likes_api import OWNER, add_recipes

from receipes.api.dependencies import SqlTaskStore
from receipes.config import Settings
from receipes.db.models import GenerationTask, PromptTemplate, Recipe
from receipes.domain.errors import REPLACEMENT_TIMED_OUT
from receipes.domain.status import TaskKind, TaskStatus
from receipes.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

Factory = async_sessionmaker[AsyncSession]
MENU_REQUEST = {
    "peopleCount": 4,
    "weeklyBudget": 1300.5,
    "habits": [3],
    "kitchenTools": [2],
}
REPLACE_TEMPLATE = (
    "Ровно один рецепт на {people_count} чел., бюджет {weekly_budget} руб. "
    "Вместо {replaced_recipe}. Меню: {existing_recipes}.\n\nВерни только JSON."
)
REPLACEMENT = {"recipes": [sample_recipe("Омлет с томатами", "Яйцо", "Томаты")]}


def replacement_reply() -> httpx.Response:
    return httpx.Response(200, json=ollama_reply(REPLACEMENT))


class Scenario:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory
        self.app: FastAPI
        self.client: httpx.AsyncClient
        self.model_requests: list[dict[str, Any]] = []
        self.replace: Callable[[], Awaitable[httpx.Response]] = self._replacement

    @staticmethod
    async def _replacement() -> httpx.Response:
        return replacement_reply()

    async def ollama(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.model_requests.append(body)
        if body["format"]["properties"]["recipes"].get("maxItems") == 1:
            return await self.replace()
        return httpx.Response(200, json=ollama_reply(sample_menu()))

    async def menu(self) -> str:
        response = await self.client.post("/generate-menu", json=MENU_REQUEST)
        assert response.status_code == 202
        return response.json()["task_id"]

    async def recipes(self, task_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/generate-menu/{task_id}")
        assert response.status_code == 200 and response.json()["status"] == "done"
        return {item["recipeId"]: item for item in response.json()["menu"]["recipes"]}

    async def completed_menu(self) -> tuple[str, dict[str, Any]]:
        task_id = await self.menu()
        assert await self.app.state.worker.process_one() is True
        return task_id, await self.recipes(task_id)

    async def post_replace(self, task_id: str, recipe_id: str) -> httpx.Response:
        return await self.client.post(
            f"/generate-menu/{task_id}/replace-recipe", json={"recipeId": recipe_id}
        )

    async def replacement_tasks(self) -> list[GenerationTask]:
        async with self.factory() as session:
            tasks = await session.scalars(
                select(GenerationTask).where(GenerationTask.kind == TaskKind.REPLACE)
            )
            return list(tasks)

    async def wait_for_replacements(self, count: int = 1) -> None:
        async with asyncio.timeout(2):
            while len(await self.replacement_tasks()) < count:
                await asyncio.sleep(0.005)

    def metric(self, name: str, **labels: str) -> float:
        return self.app.state.metrics_registry.get_sample_value(
            name, {"kind": "replace", **labels}
        )


@pytest.fixture
async def scenario(
    settings: Settings, session_factory: Factory
) -> AsyncIterator[Scenario]:
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                PromptTemplate(template="{people_count} {weekly_budget}"),
                PromptTemplate(kind=TaskKind.REPLACE, template=REPLACE_TEMPLATE),
            ]
        )
    rig = Scenario(session_factory)
    rig.app = create_app(
        settings.model_copy(update={"replace_wait_seconds": 3}),
        ollama_transport=httpx.MockTransport(rig.ollama),
        ms2_transport=httpx.MockTransport(Ms2Stub()),
    )
    async with (
        rig.app.router.lifespan_context(rig.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=rig.app), base_url="http://test"
        ) as client,
    ):
        rig.client = client
        yield rig


async def test_replacement_retries_invalid_model_output_then_updates_api2(
    scenario: Scenario,
) -> None:
    task_id, original = await scenario.completed_menu()
    old_id = next(iter(original))
    duplicate = next(item["name"] for key, item in original.items() if key != old_id)
    replies = iter(
        [sample_menu(), {"recipes": [sample_recipe(duplicate)]}, REPLACEMENT]
    )
    scenario.app.state.settings.generation_bad_response_retries = 2

    async def model() -> httpx.Response:
        return httpx.Response(200, json=ollama_reply(next(replies)))

    scenario.replace = model
    async with asyncio.TaskGroup() as group:
        posted = group.create_task(scenario.post_replace(task_id, old_id))
        await scenario.wait_for_replacements()
        assert await scenario.app.state.worker.process_one() is True
    response = posted.result()
    assert response.status_code == 200
    assert set(response.json()) == {"recipe"}
    replacement = response.json()["recipe"]
    new_id = replacement["recipeId"]
    assert new_id not in original
    assert replacement == {**REPLACEMENT["recipes"][0], "recipeId": new_id}
    expected = {key: item for key, item in original.items() if key != old_id}
    expected[new_id] = replacement
    assert await scenario.recipes(task_id) == expected
    assert await scenario.recipes(task_id) == expected
    async with scenario.factory() as session:
        assert await session.get(Recipe, UUID(old_id)) is not None
    assert len(scenario.model_requests) == 4
    assert (
        scenario.model_requests[0]["format"]["properties"]["recipes"]["minItems"] == 3
    )
    for request in scenario.model_requests[1:]:
        format_ = request["format"]["properties"]["recipes"]
        assert format_["minItems"] == format_["maxItems"] == 1
        prompt = request["prompt"]
        assert f"Вместо «{original[old_id]['name']}»" in prompt
        assert "Ровно один рецепт на 4 чел., бюджет 1300.5 руб." in prompt
        assert all(f"«{item['name']}»" in prompt for item in original.values())
        assert "привычки питания: «n3»" in prompt
        assert "доступная кухонная техника (не требуй другой): «n2»" in prompt
    assert scenario.metric("menu_generation_rejected_total", reason="count") == 1
    assert (
        scenario.metric("menu_generation_rejected_total", reason="duplicate_name") == 1
    )
    assert scenario.metric("menu_generation_tasks_total", status="done") == 1


async def test_replace_validation_and_missing_source(scenario: Scenario) -> None:
    task_id, original = await scenario.completed_menu()
    old_id = next(iter(original))
    invalid = await scenario.post_replace(task_id, "bad")
    assert invalid.status_code == 400
    assert invalid.json()["error"] == {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": {"fields": [{"loc": ["body", "recipeId"], "type": "uuid_parsing"}]},
    }
    missing_body = await scenario.client.post(
        f"/generate-menu/{task_id}/replace-recipe"
    )
    assert missing_body.status_code == 400
    for source, recipe in (
        ("bad", old_id),
        (str(uuid4()), old_id),
        (await scenario.menu(), old_id),
        (task_id, str(uuid4())),
    ):
        missing = await scenario.post_replace(source, recipe)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert await scenario.replacement_tasks() == []


async def test_two_requests_for_same_slot_do_not_generate_twice(
    scenario: Scenario,
) -> None:
    task_id, original = await scenario.completed_menu()
    old_id = next(iter(original))
    async with asyncio.TaskGroup() as group:
        first = group.create_task(scenario.post_replace(task_id, old_id))
        second = group.create_task(scenario.post_replace(task_id, old_id))
        await scenario.wait_for_replacements(2)
        assert await scenario.app.state.worker.process_one() is True
        assert await scenario.app.state.worker.process_one() is True
    assert sorted((first.result().status_code, second.result().status_code)) == [
        200,
        500,
    ]
    assert len(scenario.model_requests) == 2
    assert old_id not in await scenario.recipes(task_id)
    assert (await scenario.post_replace(task_id, old_id)).status_code == 404


async def test_queued_timeout_counts_once_without_starting_model(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, original = await scenario.completed_menu()
    scenario.app.state.settings.replace_wait_seconds = 4
    menu_waiting = await scenario.menu()
    create = SqlTaskStore.create_replace

    async def create_aged(
        store: SqlTaskStore, source: UUID, recipe_id: UUID
    ) -> UUID | None:
        replacement_id = await create(store, source, recipe_id)
        async with scenario.factory() as session, session.begin():
            await session.execute(
                text("""
                    UPDATE generation_task
                    SET created_at = created_at - interval '10 seconds'
                    WHERE task_id = :task_id
                """),
                {"task_id": replacement_id},
            )
        return replacement_id

    monkeypatch.setattr(SqlTaskStore, "create_replace", create_aged)
    response = await asyncio.wait_for(
        scenario.post_replace(task_id, next(iter(original))), 1
    )
    assert response.status_code == 500
    (replacement,) = await scenario.replacement_tasks()
    assert (replacement.status, replacement.error, replacement.result_recipe_id) == (
        TaskStatus.FAILED,
        REPLACEMENT_TIMED_OUT,
        None,
    )
    assert scenario.metric("menu_generation_tasks_total", status="failed") == 1
    assert (
        scenario.metric(
            "recipes_generation_task_duration_seconds_count", status="failed"
        )
        == 1
    )
    assert (
        scenario.metric(
            "recipes_generation_task_processing_seconds_count", status="failed"
        )
        == 0
    )
    assert len(scenario.model_requests) == 1
    assert await scenario.app.state.worker.process_one() is True
    assert await scenario.recipes(menu_waiting)
    assert await scenario.recipes(task_id) == original
    assert await scenario.app.state.worker.process_one() is False


async def test_late_ollama_response_cannot_mutate_after_timeout(
    scenario: Scenario,
) -> None:
    task_id, original = await scenario.completed_menu()
    scenario.app.state.settings.replace_wait_seconds = 0.6
    entered, release = asyncio.Event(), asyncio.Event()

    async def late_model() -> httpx.Response:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return replacement_reply()

    scenario.replace = late_model
    async with asyncio.TaskGroup() as group:
        posted = group.create_task(scenario.post_replace(task_id, next(iter(original))))
        await scenario.wait_for_replacements()
        processing = group.create_task(scenario.app.state.worker.process_one())
        await asyncio.wait_for(entered.wait(), 1)
        try:
            assert (await asyncio.wait_for(posted, 2)).status_code == 500
        finally:
            release.set()
    assert processing.result() is True
    (replacement,) = await scenario.replacement_tasks()
    assert replacement.status == TaskStatus.FAILED
    assert replacement.error == REPLACEMENT_TIMED_OUT
    assert replacement.result_recipe_id is None
    assert await scenario.recipes(task_id) == original
    assert scenario.metric("menu_generation_tasks_total", status="failed") == 1
    assert (
        scenario.metric(
            "recipes_generation_task_processing_seconds_count", status="failed"
        )
        == 1
    )
    assert scenario.metric("menu_generation_tasks_total", status="done") == 0


async def test_invalid_replace_template_leaves_normal_generation_available(
    scenario: Scenario,
) -> None:
    task_id, original = await scenario.completed_menu()
    async with scenario.factory() as session, session.begin():
        session.add(PromptTemplate(kind=TaskKind.REPLACE, template="broken"))
    async with asyncio.TaskGroup() as group:
        posted = group.create_task(scenario.post_replace(task_id, next(iter(original))))
        await scenario.wait_for_replacements()
        next_task = await scenario.menu()
        assert await scenario.app.state.worker.process_one() is True
        assert len(scenario.model_requests) == 1
        assert await scenario.app.state.worker.process_one() is True
    assert posted.result().status_code == 500
    assert await scenario.recipes(next_task)
    assert await scenario.recipes(task_id) == original
    assert len(scenario.model_requests) == 2


async def test_replace_uses_source_owner_likes_not_the_callers(
    scenario: Scenario,
) -> None:
    response = await scenario.client.post(
        "/generate-menu", json=MENU_REQUEST, headers=OWNER
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    assert await scenario.app.state.worker.process_one() is True
    original = await scenario.recipes(task_id)
    await add_recipes(scenario.factory, UUID(int=11), UUID(int=12))
    other = {"X-User-Id": "7"}
    for index, headers in ((11, OWNER), (12, other)):
        response = await scenario.client.post(
            f"/recipes/{UUID(int=index)}/like", headers=headers
        )
        assert response.status_code == 200
    async with asyncio.TaskGroup() as group:
        posted = group.create_task(
            scenario.client.post(
                f"/generate-menu/{task_id}/replace-recipe",
                json={"recipeId": next(iter(original))},
                headers=other,
            )
        )
        await scenario.wait_for_replacements()
        assert await scenario.app.state.worker.process_one() is True
    assert posted.result().status_code == 200
    [_, replace_request] = scenario.model_requests
    prompt = replace_request["prompt"]
    [data] = [line for line in prompt.splitlines() if line.startswith("[")]
    assert json.loads(data) == ["Блюдо 11"]
    assert "Ограничения пользователя и запрет повторов важнее" in prompt
    assert (await scenario.replacement_tasks())[0].prompt == prompt

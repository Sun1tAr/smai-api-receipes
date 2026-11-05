import os
from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receipes.config import Settings
from receipes.db.models import (
    GenerationParameterAllergen,
    GenerationParameterHabit,
    GenerationParameterKitchenTool,
    GenerationParameters,
    GenerationTask,
    PromptTemplate,
)
from receipes.db.repositories import TaskRepository
from receipes.domain.params import GenerationParams

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

Factory = async_sessionmaker[AsyncSession]
BASE = {"peopleCount": 2, "weeklyBudget": 100}
IDS = [{"id": i, "name": f"n{i}"} for i in range(1, 7)]
DICTIONARIES = {
    "habits": IDS,
    "diets": IDS,
    "allergies": IDS,
    "kitchenTools": IDS,
}


# Справочники MS-2 подменяются: handler.response меняется в тесте.
class Ms2Stub:
    def __init__(self) -> None:
        self.response = httpx.Response(200, json=DICTIONARIES)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self.response


@pytest.fixture
def ms2() -> Ms2Stub:
    return Ms2Stub()


@pytest.fixture
async def api(settings: Settings, session_factory: Factory, ms2: Ms2Stub):
    from receipes.main import create_app

    app = create_app(settings, ms2_transport=httpx.MockTransport(ms2))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


async def restored(factory: Factory, task_id: str) -> GenerationParams:
    async with factory() as session:
        params = await TaskRepository(session).get_params(UUID(task_id))
    assert params is not None
    return params


async def count(factory: Factory, *models: type) -> list[int]:
    async with factory() as session:
        return [
            await session.scalar(select(func.count()).select_from(model)) or 0
            for model in models
        ]


async def test_full_selection_is_restored_by_task_id(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    response = await api.post(
        "/generate-menu",
        json={
            **BASE,
            "habits": [5, 3],
            "diet": 4,
            "allergies": [2, 1],
            "kitchenTools": [6, 1, 2],
        },
    )
    assert response.status_code == 202
    params = await restored(session_factory, response.json()["task_id"])
    assert params == GenerationParams(2, Decimal("100"), (3, 5), 4, (1, 2), (1, 2, 6))


async def test_empty_choices_are_restored_as_empty(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    response = await api.post(
        "/generate-menu",
        json={**BASE, "habits": [], "diet": None, "allergies": [], "kitchenTools": []},
    )
    assert response.status_code == 202
    params = await restored(session_factory, response.json()["task_id"])
    assert params == GenerationParams(2, Decimal("100"))
    assert await count(
        session_factory,
        GenerationParameterHabit,
        GenerationParameterAllergen,
        GenerationParameterKitchenTool,
    ) == [0, 0, 0]


async def test_duplicates_are_stored_once(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    response = await api.post(
        "/generate-menu", json={**BASE, "habits": [3, 3, 3], "kitchenTools": [1, 1]}
    )
    assert response.status_code == 202
    assert await count(
        session_factory, GenerationParameterHabit, GenerationParameterKitchenTool
    ) == [1, 1]


async def test_failed_link_insert_leaves_nothing(session_factory: Factory) -> None:
    # Значение вне int4 в обход DTO: запись связи падает, задача не создаётся.
    with pytest.raises(DBAPIError):
        async with session_factory() as session, session.begin():
            await TaskRepository(session).create(
                GenerationParams(2, Decimal("100"), habits=(1, 2**40))
            )
    assert await count(session_factory, GenerationParameters) == [0]


async def test_unknown_id_is_rejected_and_stores_nothing(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    response = await api.post(
        "/generate-menu", json={**BASE, "habits": [1, 99], "kitchenTools": []}
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["fields"] == [
        {"loc": ["body", "habits", "1"], "type": "unknown_id"}
    ]
    assert await count(session_factory, GenerationTask, GenerationParameters) == [0, 0]


async def test_ms2_failure_stores_nothing(
    api: httpx.AsyncClient, session_factory: Factory, ms2: Ms2Stub
) -> None:
    ms2.response = httpx.Response(500)
    response = await api.post(
        "/generate-menu", json={**BASE, "habits": [1], "kitchenTools": []}
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert await count(
        session_factory,
        GenerationTask,
        GenerationParameters,
        GenerationParameterHabit,
    ) == [0, 0, 0]


async def test_v1_style_request_is_rejected_and_stores_nothing(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    response = await api.post("/generate-menu", json=BASE)
    assert response.status_code == 400
    assert await count(session_factory, GenerationParameters) == [0]


async def saved_prompts(
    factory: Factory, *responses: httpx.Response
) -> list[str | None]:
    async with factory() as session:
        return [
            await session.scalar(
                select(GenerationTask.prompt).where(
                    GenerationTask.task_id == UUID(item.json()["task_id"])
                )
            )
            for item in responses
        ]


async def test_prompt_carries_names_of_selected_v2_parameters(
    api: httpx.AsyncClient, session_factory: Factory, ms2: Ms2Stub
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            PromptTemplate(template="{people_count} человек, {weekly_budget} рублей")
        )
    plain = await api.post(
        "/generate-menu", json={**BASE, "habits": [], "kitchenTools": []}
    )
    assert ms2.calls == 0
    rich = await api.post(
        "/generate-menu",
        json={**BASE, "habits": [1], "diet": 2, "allergies": [3], "kitchenTools": [4]},
    )
    # Названия и проверка ID из одного запроса к MS-2.
    assert ms2.calls == 1
    prompts = await saved_prompts(session_factory, plain, rich)
    assert prompts[0] == "2 человек, 100 рублей"
    assert prompts[1] == (
        "2 человек, 100 рублей\n\n"
        "Ограничения пользователя (обязательны к выполнению):\n"
        "- привычки питания: «n1»;\n"
        "- диета: «n2»;\n"
        "- аллергены (не использовать ни в одном блюде и ингредиенте): «n3»;\n"
        "- доступная кухонная техника (не требуй другой): «n4»."
    )
    result = await api.get(f"/generate-menu/{rich.json()['task_id']}")
    assert result.json() == {"status": "pending", "menu": None, "error": None}


async def test_same_parameters_give_the_same_prompt(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    async with session_factory() as session, session.begin():
        session.add(PromptTemplate(template="{people_count} {weekly_budget}"))
    body = {**BASE, "habits": [3, 1], "diet": 2, "allergies": [], "kitchenTools": [6]}
    first = await api.post("/generate-menu", json=body)
    second = await api.post("/generate-menu", json={**body, "habits": [1, 3, 3]})
    prompts = await saved_prompts(session_factory, first, second)
    assert prompts[0] == prompts[1] and prompts[0] is not None


async def test_without_template_the_task_waits_for_the_worker(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    response = await api.post(
        "/generate-menu", json={**BASE, "habits": [1], "kitchenTools": []}
    )
    assert response.status_code == 202
    assert await saved_prompts(session_factory, response) == [None]


async def test_migration_keeps_v1_tasks_and_downgrade_drops_v2_data(
    settings: Settings, alembic: Callable[..., str], schema: None
) -> None:
    from receipes.database import create_engine

    engine = create_engine(settings)
    try:
        alembic("downgrade", "0003_task_status_changed_at")
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE generation_task, generation_parameters CASCADE")
            )
            await connection.execute(
                text(
                    "INSERT INTO generation_parameters"
                    " (id, people_count, weekly_budget)"
                    " VALUES ('00000000-0000-0000-0000-000000000001', 3, 500)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO generation_task (task_id, status, parameters_id,"
                    " created_at) VALUES ('00000000-0000-0000-0000-0000000000a1',"
                    " 'pending', '00000000-0000-0000-0000-000000000001', now())"
                )
            )
        alembic("upgrade", "head")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            params = await TaskRepository(session).get_params(
                UUID("00000000-0000-0000-0000-0000000000a1")
            )
        assert params == GenerationParams(3, Decimal("500"))

        async with factory() as session, session.begin():
            await TaskRepository(session).create(
                GenerationParams(2, Decimal("1"), (1,), 2, (3,), (4,))
            )
        alembic("downgrade", "0003_task_status_changed_at")
        async with engine.connect() as connection:
            tables = set(
                await connection.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
            columns = set(
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'generation_parameters'"
                    )
                )
            )
        assert not any(name.startswith("generation_parameter_") for name in tables)
        assert "diet_id" not in columns
    finally:
        alembic("upgrade", "head")
        await engine.dispose()


def test_alembic_has_a_single_head(alembic: Callable[..., str]) -> None:
    output = alembic("heads")
    assert output.count("(head)") == 1
    assert "0006_recipe_replacement" in output

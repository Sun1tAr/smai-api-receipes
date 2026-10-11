import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from receipes.domain import GenerationParameters
from receipes.generated_menu import GeneratedMenu
from receipes.repositories import TaskRepository


def recipe_data(name: str, ingredients: list[tuple[str, int, str]] | None = None):
    return {
        "name": name,
        "ingredients": [
            {"name": n, "quantity": q, "unit": u}
            for n, q, u in (ingredients or [("Молоко", 500, "мл"), ("Мука", 100, "г")])
        ],
        "calories": 320,
        "nutrition": {"calories": 320, "proteins": 12, "fats": 8, "carbohydrates": 50},
        "cookingTime": 15,
        "instructions": ["Первый шаг.", "Второй шаг.", "Третий шаг."],
    }


def menu_data(names: list[str] | None = None, **ingredients) -> GeneratedMenu:
    names = names or ["Каша", "Суп", "Салат"]
    return GeneratedMenu.model_validate(
        {"recipes": [recipe_data(name, ingredients.get(name)) for name in names]}
    )


def parameters(people: int = 2, budget: str = "1500.50") -> GenerationParameters:
    return GenerationParameters(people, Decimal(budget))


async def make_task(session: AsyncSession, age: timedelta | None = None):
    task = await TaskRepository(session).create(parameters())
    if age is not None:
        await session.execute(
            text(
                "UPDATE generation_task "
                "SET created_at = now() - CAST(:age AS interval) WHERE task_id = :id"
            ),
            {"age": age, "id": task.task_id},
        )
    return task


# Задача в in_progress без claim_next: старт задаёт тест (возраст в минутах).
async def start(
    session: AsyncSession, task_id: uuid.UUID, age: timedelta | None = None
):
    await session.execute(
        text(
            "UPDATE generation_task SET status = 'in_progress', prompt = 'p', "
            "started_at = now() - CAST(:age AS interval) WHERE task_id = :id"
        ),
        {"age": age or timedelta(0), "id": task_id},
    )


async def scalar(session: AsyncSession, sql: str, **params):
    return await session.scalar(text(sql), params)


def new_id() -> uuid.UUID:
    return uuid.uuid4()

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictInt,
    WithJsonSchema,
    field_validator,
)
from pydantic_core import PydanticCustomError

MAX_PEOPLE = 20
MAX_BUDGET = 1_000_000
MAX_HABITS = 3
INT4_MIN = -(2**31)
INT4_MAX = 2**31 - 1

# Идентификатор справочника MS-2. Диапазон int4 — это тип колонки в БД; существование
# ID (в том числе нулевых и отрицательных) проверяется по справочникам MS-2.
DictionaryId = Annotated[StrictInt, Field(ge=INT4_MIN, le=INT4_MAX)]


# Целые значения отдаются как `35`, дробные как `35.5` (не `35.0`): клиент с Int
# иначе не разберёт ответ. В схеме OpenAPI это просто `number`.
def _json_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


Number = Annotated[
    Decimal,
    PlainSerializer(_json_number, return_type=int | float, when_used="json"),
    WithJsonSchema({"type": "number"}),
]


class GenerateMenuRequest(BaseModel):
    # Только camelCase из контракта: snake_case (`people_count`) не принимается.
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "peopleCount": 4,
                "weeklyBudget": 5000,
                "habits": [3, 5],
                "diet": 4,
                "allergies": [1, 2],
                "kitchenTools": [1, 2, 6],
            }
        },
    )

    people_count: int = Field(
        alias="peopleCount",
        strict=True,
        ge=1,
        le=MAX_PEOPLE,
        description=(
            f"Количество человек, целое число от 1 до {MAX_PEOPLE}. "
            "Строки, `true` и `4.0` отклоняются."
        ),
    )
    weekly_budget: float = Field(
        alias="weeklyBudget",
        strict=True,
        gt=0,
        le=MAX_BUDGET,
        allow_inf_nan=False,
        description=(
            f"Недельный бюджет в рублях: число больше 0 и не больше {MAX_BUDGET}, "
            "не более двух знаков после запятой. Строки отклоняются."
        ),
    )

    habits: list[DictionaryId] = Field(
        description=(
            "Идентификаторы пищевых привычек из справочника MS-2 (API-7). Поле "
            f"обязательно, но выбор может быть пустым: `[]`. Не больше {MAX_HABITS} "
            "разных ID, иначе 400 `too_long`; повторы сохраняются один раз и за "
            "отдельные привычки не считаются. Элементы только целые числа (int4): "
            "строки, `true`, `4.0` и `null` отклоняются. ID должен существовать в "
            "справочнике MS-2, иначе 400 `unknown_id`."
        ),
        examples=[[3, 5]],
    )
    diet: DictionaryId | None = Field(
        default=None,
        description=(
            "Идентификатор диеты из справочника MS-2 или `null`. Поле можно не "
            "передавать, это то же самое, что `null`. ID должен существовать в "
            "справочнике MS-2, иначе 400 `unknown_id`."
        ),
        examples=[4],
    )
    allergies: list[DictionaryId] = Field(
        default_factory=list,
        description=(
            "Идентификаторы аллергенов из справочника MS-2. Поле можно не "
            "передавать, это то же самое, что `[]`. Повторы сохраняются один раз. "
            "ID должен существовать в справочнике MS-2, иначе 400 `unknown_id`."
        ),
        examples=[[1, 2]],
    )
    kitchen_tools: list[DictionaryId] = Field(
        alias="kitchenTools",
        description=(
            "Идентификаторы кухонной техники из справочника MS-2. Поле обязательно, "
            "но выбор может быть пустым: `[]`. Повторы сохраняются один раз. ID "
            "должен существовать в справочнике MS-2, иначе 400 `unknown_id`."
        ),
        examples=[[1, 2, 6]],
    )

    # Считаем разные ID: повторы всё равно схлопываются. Тип ошибки `too_long`, как у
    # max_length, место — само поле.
    @field_validator("habits", mode="after")
    @classmethod
    def at_most_three_habits(cls, value: list[int]) -> list[int]:
        if len(set(value)) > MAX_HABITS:
            raise PydanticCustomError(
                "too_long",
                "must have at most {max_length} distinct items",
                {"max_length": MAX_HABITS},
            )
        return value

    @field_validator("weekly_budget", mode="after")
    @classmethod
    def at_most_two_decimal_places(cls, value: float) -> float:
        exponent = Decimal(str(value)).as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -2:
            raise ValueError("must have at most 2 decimal places")
        return value


class GenerateMenuResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"task_id": "b1e2c3d4-5678-90ab-cdef-1234567890ab"}
        }
    )

    task_id: str = Field(
        description="Идентификатор задачи; по нему запрашивается результат (API-2)."
    )


class IngredientResponse(BaseModel):
    name: str = Field(description="Название ингредиента без количества и единицы.")
    quantity: Number = Field(
        description="Количество на всё блюдо для указанного числа человек, больше 0."
    )
    unit: str = Field(description="Единица измерения: г, мл, шт., ч.л. и т.п.")


class NutritionResponse(BaseModel):
    calories: Number = Field(description="Калорийность одной порции, ккал.")
    proteins: Number = Field(description="Белки одной порции, г.")
    fats: Number = Field(description="Жиры одной порции, г.")
    carbohydrates: Number = Field(description="Углеводы одной порции, г.")


class RecipeResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recipe_id: UUID = Field(
        alias="recipeId", description="Идентификатор сохранённого рецепта."
    )

    name: str = Field(description="Название блюда.")
    ingredients: list[IngredientResponse] = Field(
        description=(
            "Ингредиенты, не менее одного. Порядок не гарантируется: "
            "полагаться на него нельзя."
        ),
    )
    calories: Number = Field(
        description=(
            "Калорийность одной порции, ккал, как её вернула модель. Может "
            "расходиться с `nutrition.calories`; основным считайте "
            "`nutrition.calories`."
        )
    )
    nutrition: NutritionResponse = Field(description="КБЖУ одной порции.")
    cooking_time: Number = Field(
        alias="cookingTime", description="Время приготовления, минут."
    )
    instructions: list[str] = Field(
        description="Шаги приготовления по порядку, не менее одного."
    )
    photo: str | None = Field(
        default=None,
        description="Ссылка на фотографию. В версии 1 всегда `null`.",
    )


class MenuResponse(BaseModel):
    recipes: list[RecipeResponse] = Field(
        description=(
            "Рецепты меню, не менее 3. Число блюд не фиксировано, разбивки по дням "
            "и приёмам пищи нет, порядок рецептов не гарантируется."
        )
    )


class TaskResultResponse(BaseModel):
    status: Literal["pending", "in_progress", "done", "failed"] = Field(
        description=(
            "Статус задачи: `pending` (ждёт очереди), `in_progress` (идёт генерация), "
            "`done` (меню готово), `failed` (ошибка, см. `error`)."
        )
    )
    menu: MenuResponse | None = Field(
        description="Результат генерации; `null`, если статус не `done`."
    )
    error: str | None = Field(
        description=(
            "Причина ошибки; `null`, если статус не `failed`. Одно из: "
            "`Neurocore unavailable`, `Neurocore timeout`, "
            "`Neurocore model not found`, `Neurocore error`, "
            "`Invalid neurocore response`, "
            "`Neurocore response truncated`, `Service restarted`, "
            "`Menu generation failed`."
        )
    )


class SavedParametersResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "habits": [3, 5],
                "diet": 4,
                "allergies": [1, 2],
                "kitchenTools": [1, 2, 6],
            }
        }
    )

    habits: list[int] = Field(description="ID привычек по возрастанию; `[]`, если нет.")
    diet: int | None = Field(description="ID диеты или `null`.")
    allergies: list[int] = Field(description="ID аллергенов по возрастанию.")
    kitchen_tools: list[int] = Field(
        alias="kitchenTools", description="ID кухонной техники по возрастанию."
    )

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
)

MAX_PEOPLE = 20
MAX_BUDGET = 1_000_000


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
        json_schema_extra={"example": {"peopleCount": 4, "weeklyBudget": 5000}},
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

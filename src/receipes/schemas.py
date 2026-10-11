from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    WithJsonSchema,
    field_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

from receipes.domain import (
    PEOPLE_COUNT_MAX,
    PEOPLE_COUNT_MIN,
    WEEKLY_BUDGET_MAX,
    TaskStatus,
    capitalize_ingredient_name,
)
from receipes.models import Menu

BUDGET_SCHEMA = {
    "type": "number",
    "exclusiveMinimum": 0,
    "maximum": float(WEEKLY_BUDGET_MAX),
    "description": "Недельный бюджет в рублях, не более двух знаков после запятой.",
    "examples": [5000, 1500.5],
}


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class GenerateMenuRequest(BaseModel):
    # Только camelCase из контракта (snake_case не принимается) и никаких лишних полей.
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        json_schema_extra={"example": {"peopleCount": 4, "weeklyBudget": 5000}},
    )

    people_count: StrictInt = Field(
        ge=PEOPLE_COUNT_MIN,
        le=PEOPLE_COUNT_MAX,
        description="Количество человек, от 1 до 20.",
        examples=[4],
    )
    weekly_budget: Annotated[
        Decimal,
        Field(
            gt=0,
            le=WEEKLY_BUDGET_MAX,
            decimal_places=2,
            allow_inf_nan=False,
        ),
        WithJsonSchema(BUDGET_SCHEMA),
    ]

    # Бюджет — только JSON-число: строка «100» или true не принимаются.
    @field_validator("weekly_budget", mode="before")
    @classmethod
    def only_json_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            raise PydanticCustomError("number_type", "must be a number")
        return value


class TaskCreatedResponse(BaseModel):
    task_id: str = Field(
        description="Идентификатор задачи (UUID) для запроса статуса.",
        examples=["b1e2c3d4-5678-90ab-cdef-1234567890ab"],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"task_id": "b1e2c3d4-5678-90ab-cdef-1234567890ab"}
        }
    )


class IngredientResponse(CamelModel):
    name: str
    quantity: float
    unit: str


class NutritionResponse(CamelModel):
    calories: float
    proteins: float
    fats: float
    carbohydrates: float


class RecipeResponse(CamelModel):
    name: str
    ingredients: list[IngredientResponse]
    calories: float = Field(description="Калорийность, ккал.")
    nutrition: NutritionResponse
    cooking_time: float = Field(description="Время готовки, мин.")
    instructions: list[str]
    photo: str | None = Field(
        default=None, description="Ссылка на фотографию; пока всегда null."
    )


class MenuResponse(CamelModel):
    recipes: list[RecipeResponse]


class TaskStatusResponse(BaseModel):
    status: TaskStatus
    menu: MenuResponse | None = Field(
        description="Результат генерации; null, если status не done."
    )
    error: str | None = Field(
        description="Описание ошибки; null, если status не failed."
    )


# Числа в ответе — JSON-числа (float), а не строки Decimal. Имена ингредиентов
# показываются с заглавной первой буквой; в БД они не меняются.
def menu_response(menu: Menu) -> MenuResponse:
    recipes = []
    for link in menu.menu_recipes:
        recipe = link.recipe
        nutrition = recipe.nutrition
        recipes.append(
            RecipeResponse(
                name=recipe.name,
                ingredients=[
                    IngredientResponse(
                        name=capitalize_ingredient_name(item.ingredient.name),
                        quantity=float(item.quantity),
                        unit=item.unit,
                    )
                    for item in recipe.recipe_ingredients
                ],
                calories=float(recipe.calories),
                nutrition=NutritionResponse(
                    calories=float(nutrition.calories),
                    proteins=float(nutrition.proteins),
                    fats=float(nutrition.fats),
                    carbohydrates=float(nutrition.carbohydrates),
                ),
                cooking_time=float(recipe.cooking_time),
                instructions=[step.text for step in recipe.instructions],
                photo=None,
            )
        )
    return MenuResponse(recipes=recipes)

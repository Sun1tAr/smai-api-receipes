from decimal import ROUND_HALF_UP, Decimal

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from receipes.domain import (
    INSTRUCTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
    UNIT_MAX_LENGTH,
    normalize_ingredient_name,
)

logger = structlog.get_logger()

RECIPES_MIN, RECIPES_MAX = 3, 21
INGREDIENTS_MIN, INGREDIENTS_MAX = 1, 50
STEPS_MIN, STEPS_MAX = 1, 50
CALORIES_MAX = Decimal("10000")
NUTRIENT_MAX = Decimal("10000")
COOKING_TIME_MAX = Decimal("1440")
QUANTITY_MAX = Decimal("100000")
CENT = Decimal("0.01")


class GateModel(BaseModel):
    # Поля ответа Ollama в camelCase, как в контракте API; лишние поля игнорируются.
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
    )


def _amount(maximum: Decimal) -> Decimal:
    return Field(ge=0, le=maximum, allow_inf_nan=False)


# Числа от модели округляются до сотых: так их хранит Numeric(…, 2).
def _round(cls: type, value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class GeneratedNutrition(GateModel):
    calories: Decimal = _amount(NUTRIENT_MAX)
    proteins: Decimal = _amount(NUTRIENT_MAX)
    fats: Decimal = _amount(NUTRIENT_MAX)
    carbohydrates: Decimal = _amount(NUTRIENT_MAX)

    _rounded = field_validator("*", mode="after")(_round)


class GeneratedIngredient(GateModel):
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    quantity: Decimal = _amount(QUANTITY_MAX)
    unit: str = Field(min_length=1, max_length=UNIT_MAX_LENGTH)

    _rounded = field_validator("quantity", mode="after")(_round)

    @field_validator("name", mode="after")
    @classmethod
    def normalized_name_fits(cls, value: str) -> str:
        # casefold может удлинить строку (ß → ss), а в колонке хранится результат.
        normalized = normalize_ingredient_name(value)
        if not normalized or len(normalized) > NAME_MAX_LENGTH:
            raise ValueError("normalized name is empty or too long")
        return value

    @property
    def normalized_name(self) -> str:
        return normalize_ingredient_name(self.name)


class GeneratedRecipe(GateModel):
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    ingredients: list[GeneratedIngredient] = Field(
        min_length=INGREDIENTS_MIN, max_length=INGREDIENTS_MAX
    )
    calories: Decimal = _amount(CALORIES_MAX)
    nutrition: GeneratedNutrition
    cooking_time: Decimal = _amount(COOKING_TIME_MAX)
    instructions: list[str] = Field(min_length=STEPS_MIN, max_length=STEPS_MAX)
    # Фотографию модель выдумать не может: поле принимается, но не хранится.
    photo: None = Field(default=None, exclude=True)

    _rounded = field_validator("calories", "cooking_time", mode="after")(_round)

    @field_validator("photo", mode="before")
    @classmethod
    def ignore_photo(cls, value: object) -> None:
        return None

    @field_validator("instructions", mode="after")
    @classmethod
    def steps_are_text(cls, steps: list[str]) -> list[str]:
        for step in steps:
            if not step.strip() or len(step) > INSTRUCTION_MAX_LENGTH:
                raise ValueError("step is empty or too long")
        return steps

    # Дубли по нормализованному имени: одна единица — складываем количества,
    # разная — оставляем первое вхождение.
    @model_validator(mode="after")
    def merge_duplicate_ingredients(self) -> "GeneratedRecipe":
        merged: dict[str, GeneratedIngredient] = {}
        for ingredient in self.ingredients:
            key = ingredient.normalized_name
            first = merged.get(key)
            if first is None:
                merged[key] = ingredient.model_copy()
            elif first.unit.casefold() == ingredient.unit.casefold():
                first.quantity += ingredient.quantity
            else:
                logger.warning("duplicate_ingredient_dropped", ingredient=key)
        self.ingredients = list(merged.values())
        return self


class GeneratedMenu(GateModel):
    recipes: list[GeneratedRecipe] = Field(
        min_length=RECIPES_MIN, max_length=RECIPES_MAX
    )

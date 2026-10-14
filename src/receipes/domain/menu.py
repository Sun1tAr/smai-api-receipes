from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


# Меню, пришедшее от модели, в виде, пригодном для сохранения. Правила проверки
# ответа модели («ворота») добавляются вместе с воркером (SMAI-54).
class NutritionPayload(BaseModel):
    calories: Decimal
    proteins: Decimal
    fats: Decimal
    carbohydrates: Decimal


class IngredientPayload(BaseModel):
    name: str
    quantity: Decimal
    unit: str


class RecipePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    ingredients: list[IngredientPayload]
    calories: Decimal
    nutrition: NutritionPayload
    cooking_time: Decimal = Field(alias="cookingTime")
    instructions: list[str]


class MenuPayload(BaseModel):
    recipes: list[RecipePayload]

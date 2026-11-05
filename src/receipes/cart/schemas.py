from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from receipes.api.schemas import IngredientResponse, Number
from receipes.cart.calculation import Unit


class CartRequest(BaseModel):
    recipe_id: UUID = Field(
        alias="recipeId",
        description="UUID сохранённого рецепта из результата API-2.",
        examples=["b1e2c3d4-5678-90ab-cdef-1234567890ab"],
    )


class CartPrice(BaseModel):
    current: Number = Field(description="Цена за единицу товара.")
    currency: Literal["RUB"] = Field(description="Валюта цены, RUB.")


class CartWeight(BaseModel):
    value: Number = Field(description="Вес упаковки в килограммах.")
    unit: Literal["кг"] = Field(description="Единица веса, кг.")


class CartProduct(BaseModel):
    id: int = Field(description="ID товара в каталоге ВкусВилла.")
    name: str = Field(description="Название товара.")
    price: CartPrice
    unit: Unit = Field(description="Единица продажи товара: кг или шт.")
    weight: CartWeight | None = Field(description="Вес упаковки, если известен.")


class CartMatch(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    ingredient: IngredientResponse
    product: CartProduct | None = Field(default=None, description="Подобранный товар.")
    q: Number | None = Field(
        default=None,
        description="Количество в единицах продажи. Ограничено значением 40.",
    )
    warning: str | None = Field(
        default=None, description="Предупреждение о количестве."
    )
    error: str | None = Field(
        default=None, description="Причина отсутствия совпадения."
    )


class CartResponse(BaseModel):
    matches: list[CartMatch] = Field(description="Результат для каждого ингредиента.")
    total: Number = Field(
        description="Сумма цены × q по подобранным товарам, в рублях."
    )
    all_matched: bool = Field(
        serialization_alias="allMatched",
        description="Все ингредиенты сопоставлены с товарами.",
    )
    unmatched_count: int = Field(
        serialization_alias="unmatchedCount",
        description="Число позиций с ошибкой.",
    )

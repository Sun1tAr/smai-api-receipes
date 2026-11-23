import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from receipes.domain.errors import InvalidNeurocoreResponse
from receipes.domain.names import name_key, normalize_name
from receipes.domain.status import TaskKind

MIN_RECIPES = 3
MAX_NAME_LENGTH = 200
MAX_STEP_LENGTH = 2000
MAX_INGREDIENTS = 50
MAX_STEPS = 30
MAX_NUMBER = Decimal("99999999.99")
_CENT = Decimal("0.01")
# Запрещённые категории Unicode: управляющие, форматирующие, разделители строк/абзацев.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# Нумерация в начале шага: «1.», «2)»; десятичное «1.5 часа» не трогаем.
_STEP_NUMBERING = re.compile(r"^\s*\d+\s*[.)](?!\d)\s*")
# Название вида «200 г муки»: количество и единица в начале; «Масло 82%» допустимо.
_QUANTITY_PREFIX = re.compile(
    r"^\d+(?:[.,]\d+)?(?:\s*[-–/]\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?:кг|гр?|грамм\w*|килограмм\w*|мл|л|литр\w*|милилитр\w*|шт|штук\w*|"
    r"ч\.?\s?л|ст\.?\s?л|стакан\w*|ложк\w*|щепот\w*|пучк\w*|зубч\w*)(?!\w)",
    re.IGNORECASE,
)


# Число из JSON: только int/float (не строка и не bool), конечное, до 2 знаков.
def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ValueError("must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("must be finite")
    number = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    if not number.is_finite():
        raise ValueError("must be finite")
    try:
        return number.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        # Число не помещается в точность Decimal (1e30, 10**40): это выход за границы,
        # а не сбой схемы; исключение уходит мимо pydantic как есть.
        raise InvalidNeurocoreResponse("bounds") from None


Number = Annotated[Decimal, BeforeValidator(_number)]


# Меню, пришедшее от модели, в виде, пригодном для сохранения. Поле photo модели
# не читается: в v1 фото не реализовано, значение всегда NULL.
class NutritionPayload(BaseModel):
    calories: Number
    proteins: Number
    fats: Number
    carbohydrates: Number


class IngredientPayload(BaseModel):
    name: str
    quantity: Number
    unit: str


class RecipePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    ingredients: list[IngredientPayload]
    calories: Number
    nutrition: NutritionPayload
    cooking_time: Number = Field(alias="cookingTime")
    instructions: list[str]


class MenuPayload(BaseModel):
    recipes: list[RecipePayload]


def _reject(reason: str) -> InvalidNeurocoreResponse:
    return InvalidNeurocoreResponse(reason)


def _check_text(raw: str) -> str:
    if any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in raw):
        raise _reject("text")
    return raw


def _clean_name(raw: str, *, limit: int = MAX_NAME_LENGTH) -> str:
    name = normalize_name(_check_text(raw))
    if not name:
        raise _reject("text")
    if len(name) > limit:
        raise _reject("bounds")
    return name


def _clean_step(raw: str) -> str:
    step = _STEP_NUMBERING.sub("", _check_text(raw), count=1).strip()
    if not step:
        raise _reject("text")
    if len(step) > MAX_STEP_LENGTH:
        raise _reject("bounds")
    return step


def _check_numbers(recipe: RecipePayload) -> None:
    values = [
        recipe.calories,
        recipe.cooking_time,
        *(getattr(recipe.nutrition, field) for field in NutritionPayload.model_fields),
        *(item.quantity for item in recipe.ingredients),
    ]
    if any(not Decimal(0) <= value <= MAX_NUMBER for value in values):
        raise _reject("bounds")
    if any(item.quantity <= 0 for item in recipe.ingredients):
        raise _reject("bounds")


def _clean_recipe(recipe: RecipePayload) -> RecipePayload:
    if not recipe.ingredients or not recipe.instructions:
        raise _reject("count")
    if (
        len(recipe.ingredients) > MAX_INGREDIENTS
        or len(recipe.instructions) > MAX_STEPS
    ):
        raise _reject("bounds")
    recipe.name = _clean_name(recipe.name)
    seen: set[str] = set()
    for item in recipe.ingredients:
        item.name = _clean_name(item.name)
        item.unit = _clean_name(item.unit)
        if _QUANTITY_PREFIX.match(item.name):
            raise _reject("ingredient_name")
        key = name_key(item.name)
        if key in seen:
            raise _reject("duplicate_ingredient")
        seen.add(key)
    recipe.instructions = [_clean_step(step) for step in recipe.instructions]
    _check_numbers(recipe)
    return recipe


# «Ворота» ответа модели: типы, количество, границы, чистота текста, дубли.
# Нарушение — InvalidNeurocoreResponse с причиной для метрики.
def parse_menu(
    data: object,
    *,
    kind: TaskKind = TaskKind.MENU,
    existing_names: tuple[str, ...] = (),
) -> MenuPayload:
    try:
        payload = MenuPayload.model_validate(data, strict=False)
    except ValidationError:
        raise _reject("schema") from None
    count = len(payload.recipes)
    if not (count == 1 if kind == TaskKind.REPLACE else count >= MIN_RECIPES):
        raise _reject("count")
    names = {name_key(name) for name in existing_names}
    for recipe in payload.recipes:
        _clean_recipe(recipe)
        key = name_key(recipe.name)
        if key in names:
            raise _reject("duplicate_name")
        names.add(key)
    return payload

import asyncio
import math
import re
from decimal import Decimal
from html import unescape
from typing import Any

from receipes.api.schemas import IngredientResponse
from receipes.cart.calculation import Unit, cart_quantity, normalize_quantity
from receipes.cart.mcp import McpClient, McpError
from receipes.cart.schemas import (
    CartMatch,
    CartPrice,
    CartProduct,
    CartResponse,
    CartWeight,
)
from receipes.domain.names import name_key

_SEARCH = "vkusvill_products_search"
_DETAILS = "vkusvill_product_details"
_NOT_FOUND = "Товар не найден"
_CATALOG_ERROR = "Каталог недоступен"
_INCOMPATIBLE = "несовместимые единицы"
_UNSUPPORTED_UNIT = "единица не конвертируется"

# shortcut: узкие совпадения, расширять только по проверенным примерам каталога
_FACET_ALIASES = {
    "морковь": "овощи, фрукты, ягоды, зелень",
    "молоко": "молоко, сливки",
    "молоко безлактозное": "молоко, сливки",
}
_PACKAGE = re.compile(r"(?:\s*,\s*|\s+)\d+(?:[.,]\d+)?\s*(?:кг|гр?|мл|л|шт)\Z")
_MILK_NAME = re.compile(
    r"молоко(?: коровье)?(?P<lactose_free> безлактозное)?"
    r"(?: цельное| пастеризованное| ультрапастеризованное)?"
    r"(?: [0-9](?:[.,][0-9]+)?%)?(?: в бутылке)?"
)


class CatalogUnavailable(Exception):
    pass


def _category_id(data: dict[str, Any], ingredient: str) -> int | None:
    facets = data.get("facets")
    categories = facets.get("categories") if isinstance(facets, dict) else None
    if not isinstance(categories, list):
        return None
    titles = {ingredient, _FACET_ALIASES.get(ingredient)}
    matches = set()
    for category in categories:
        if not isinstance(category, dict):
            continue
        category_id = category.get("category_id")
        if type(category_id) is not int or category_id <= 0:
            continue
        names = (category.get("category_name"), category.get("subcategory_name"))
        if any(
            isinstance(name, str) and name_key(unescape(name)) in titles
            for name in names
        ):
            matches.add(category_id)
    return matches.pop() if len(matches) == 1 else None


def _name_matches(name: str, ingredient: str) -> bool:
    title = _PACKAGE.sub("", name_key(unescape(name)))
    if ingredient in ("молоко", "молоко безлактозное"):
        milk = _MILK_NAME.fullmatch(title)
        return milk is not None and bool(milk["lactose_free"]) == (
            ingredient == "молоко безлактозное"
        )
    return title == ingredient or (ingredient == "морковь" and title == "морковь мытая")


def _product_unit(value: Any) -> Unit | None:
    if value == "кг":
        return "кг"
    if value in ("шт", "шт."):
        return "шт"
    return None


def _number(value: Any) -> Decimal | None:
    if type(value) not in (Decimal, int, float):
        return None
    amount = Decimal(str(value))
    return amount if amount.is_finite() and math.isfinite(float(amount)) else None


def _product(
    data: dict[str, Any], product_id: int, ingredient: str
) -> CartProduct | None:
    actual_id = data.get("id")
    title = data.get("name")
    unit = _product_unit(data.get("unit"))
    price = data.get("price")
    if (
        type(actual_id) is not int
        or actual_id != product_id
        or data.get("available", True) is not True
        or not isinstance(title, str)
        or not _name_matches(title, ingredient)
        or unit is None
        or not isinstance(price, dict)
        or price.get("currency") != "RUB"
    ):
        return None
    current = _number(price.get("current"))
    if current is None or current < 0:
        return None
    raw_weight = data.get("weight")
    weight = None
    if raw_weight is not None:
        if not isinstance(raw_weight, dict) or raw_weight.get("unit") != "кг":
            return None
        value = _number(raw_weight.get("value"))
        if value is None or value <= 0:
            return None
        weight = CartWeight(value=value, unit="кг")
    return CartProduct(
        id=product_id,
        name=unescape(title),
        price=CartPrice(current=current, currency="RUB"),
        unit=unit,
        weight=weight,
    )


async def _match(
    mcp: McpClient,
    ingredient: IngredientResponse,
    amount: Decimal,
    unit: Unit,
) -> CartMatch | None:
    key = name_key(ingredient.name)
    query = {
        "q": ingredient.name,
        "vvonly": 1,
        "sort": "popularity",
        "page": 1,
        "mode": "custom",
        "fields": ["id", "name", "price", "unit", "weight"],
    }
    try:
        first_search = await mcp.call(_SEARCH, query)
    except McpError as error:
        if error.unavailable:
            # None означает сбой инфраструктуры до получения первого ответа.
            return None
        return CartMatch(ingredient=ingredient, error=_CATALOG_ERROR)

    category_id = _category_id(first_search, key)
    if category_id is None:
        return CartMatch(ingredient=ingredient, error=_NOT_FOUND)
    try:
        second_search = await mcp.call(_SEARCH, {**query, "category_id": category_id})
        items = second_search.get("items")
        incompatible = False
        for item in items[:10] if isinstance(items, list) else ():
            if not isinstance(item, dict):
                continue
            title = item.get("name")
            product_id = item.get("id")
            if (
                type(product_id) is not int
                or product_id <= 0
                or not isinstance(title, str)
                or not _name_matches(title, key)
            ):
                continue
            product_unit = _product_unit(item.get("unit"))
            if product_unit is None or (unit == "шт" and product_unit != unit):
                incompatible = True
                continue
            data = await mcp.call(_DETAILS, {"id": product_id})
            product = _product(data, product_id, key)
            if product is None:
                return CartMatch(ingredient=ingredient, error=_NOT_FOUND)
            quantity = cart_quantity(
                amount,
                unit,
                product.unit,
                product.weight.value if product.weight else None,
            )
            if quantity is None:
                return CartMatch(ingredient=ingredient, error=_INCOMPATIBLE)
            q, warning = quantity
            return CartMatch(
                ingredient=ingredient, product=product, q=q, warning=warning
            )
        return CartMatch(
            ingredient=ingredient, error=_INCOMPATIBLE if incompatible else _NOT_FOUND
        )
    except McpError:
        return CartMatch(ingredient=ingredient, error=_CATALOG_ERROR)


async def build_cart(
    mcp: McpClient, ingredients: list[IngredientResponse], deadline: float
) -> CartResponse:
    loop = asyncio.get_running_loop()
    matches: list[CartMatch] = []
    jobs: dict[int, asyncio.Task[CartMatch | None]] = {}
    async with asyncio.TaskGroup() as group:
        for index, ingredient in enumerate(ingredients):
            amount = normalize_quantity(ingredient.quantity, ingredient.unit)
            if amount is None:
                matches.append(
                    CartMatch(ingredient=ingredient, error=_UNSUPPORTED_UNIT)
                )
                continue
            matches.append(
                CartMatch(ingredient=ingredient, error="Время поиска истекло")
            )
            if loop.time() < deadline:
                jobs[index] = group.create_task(_match(mcp, ingredient, *amount))
        if jobs:
            _, pending = await asyncio.wait(
                jobs.values(), timeout=max(0, deadline - loop.time())
            )
            for job in pending:
                job.cancel()

    unavailable = 0
    for index, job in jobs.items():
        if job.cancelled():
            continue
        row = job.result()
        if row is None:
            unavailable += 1
            row = CartMatch(ingredient=ingredients[index], error=_CATALOG_ERROR)
        matches[index] = row
    if jobs and unavailable == len(jobs):
        raise CatalogUnavailable

    unmatched_count = sum(row.error is not None for row in matches)
    total = sum(
        (
            row.product.price.current * row.q
            for row in matches
            if row.error is None and row.product is not None and row.q is not None
        ),
        Decimal(0),
    )
    return CartResponse(
        matches=matches,
        total=total,
        all_matched=unmatched_count == 0,
        unmatched_count=unmatched_count,
    )

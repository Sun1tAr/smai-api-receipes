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
from receipes.domain.names import normalize_name
from receipes.generation.ollama import OllamaClient

_SEARCH = "vkusvill_products_search"
_DETAILS = "vkusvill_product_details"
_NOT_FOUND = "Товар не найден"
_CATALOG_ERROR = "Каталог недоступен"
_INCOMPATIBLE = "несовместимые единицы"
_UNSUPPORTED_UNIT = "единица не конвертируется"
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:[-–—]\s*(\d+(?:[.,]\d+)?)\s*)?%")


class CatalogUnavailable(Exception):
    pass


def _percentages(name: str) -> set[tuple[Decimal, Decimal]]:
    return {
        (Decimal(start.replace(",", ".")), Decimal((end or start).replace(",", ".")))
        for start, end in _PERCENT.findall(normalize_name(unescape(name)))
    }


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
    data: dict[str, Any], product_id: int, candidate_title: str
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
        or normalize_name(unescape(title)).casefold()
        != normalize_name(unescape(candidate_title)).casefold()
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


async def _search(
    mcp: McpClient, ingredient: IngredientResponse
) -> list[dict[str, Any]] | str | None:
    try:
        data = await mcp.call(
            _SEARCH,
            {
                "q": ingredient.name,
                "vvonly": 1,
                "sort": "popularity",
                "page": 1,
                "mode": "custom",
                "fields": ["id", "name", "price", "unit", "weight"],
            },
        )
    except McpError as error:
        return None if error.unavailable else _CATALOG_ERROR
    items = data.get("items")
    candidates = []
    seen = set()
    for item in items[:10] if isinstance(items, list) else ():
        if not isinstance(item, dict):
            continue
        product_id, title = item.get("id"), item.get("name")
        if (
            type(product_id) is int
            and 0 < product_id <= 999999999
            and isinstance(title, str)
            and title.strip()
            and product_id not in seen
        ):
            title = unescape(title)
            try:
                title.encode("utf-8")
            except UnicodeEncodeError:
                continue
            candidates.append({"id": product_id, "name": title})
            seen.add(product_id)
    return candidates


async def _materialize(
    mcp: McpClient,
    ingredient: IngredientResponse,
    candidates: list[dict[str, Any]],
    ids: tuple[int, ...],
) -> CartMatch:
    amount, unit = normalize_quantity(ingredient.quantity, ingredient.unit)
    titles = {item["id"]: item["name"] for item in candidates}
    incompatible = False
    catalog_error = False
    required_percentages = _percentages(ingredient.name)
    for product_id in ids:
        if (
            required_percentages
            and _percentages(titles[product_id]) != required_percentages
        ):
            continue
        try:
            data = await mcp.call(_DETAILS, {"id": product_id})
        except McpError:
            catalog_error = True
            continue
        product = _product(data, product_id, titles[product_id])
        if product is None:
            continue
        quantity = cart_quantity(
            amount,
            unit,
            product.unit,
            product.weight.value if product.weight else None,
        )
        if quantity is None:
            incompatible = True
            continue
        q, warning = quantity
        return CartMatch(ingredient=ingredient, product=product, q=q, warning=warning)
    return CartMatch(
        ingredient=ingredient,
        error=_INCOMPATIBLE
        if incompatible
        else (_CATALOG_ERROR if catalog_error else _NOT_FOUND),
    )


async def build_cart(
    mcp: McpClient,
    ollama: OllamaClient,
    ingredients: list[IngredientResponse],
    deadline: float,
) -> CartResponse:
    loop = asyncio.get_running_loop()
    matches: list[CartMatch] = []
    searches = {}
    search_deadline = min(
        deadline, loop.time() + min(10, max(0, deadline - loop.time()) * 2 / 7)
    )
    async with asyncio.TaskGroup() as group:
        for index, ingredient in enumerate(ingredients):
            if normalize_quantity(ingredient.quantity, ingredient.unit) is None:
                matches.append(
                    CartMatch(ingredient=ingredient, error=_UNSUPPORTED_UNIT)
                )
                continue
            matches.append(
                CartMatch(ingredient=ingredient, error="Время поиска истекло")
            )
            if loop.time() < search_deadline:
                searches[index] = group.create_task(_search(mcp, ingredient))
        if searches:
            _, pending = await asyncio.wait(
                searches.values(), timeout=max(0, search_deadline - loop.time())
            )
            for job in pending:
                job.cancel()

    unavailable = 0
    positions = []
    for index, job in searches.items():
        if job.cancelled():
            unavailable += 1
            continue
        candidates = job.result()
        if candidates is None:
            unavailable += 1
            matches[index].error = _CATALOG_ERROR
        elif isinstance(candidates, str):
            matches[index].error = candidates
        elif not candidates:
            matches[index].error = _NOT_FOUND
        else:
            positions.append(
                {
                    "index": index,
                    "ingredient": ingredients[index].name,
                    "candidates": candidates,
                }
            )
    if searches and unavailable == len(searches):
        raise CatalogUnavailable

    if positions:
        remaining = max(0, deadline - loop.time())
        # Reserve up to 10s for a details call and its existing MCP retry.
        model_deadline = loop.time() + min(
            15, max(0, remaining - min(10, remaining * 0.4))
        )
        selections = await ollama.match_cart(positions, model_deadline)
        jobs = {}
        async with asyncio.TaskGroup() as group:
            for position in positions:
                index = position["index"]
                selection = selections[index]
                if selection.error:
                    matches[index].error = selection.error
                elif loop.time() < deadline:
                    matches[index].error = "Время проверки товара истекло"
                    jobs[index] = group.create_task(
                        _materialize(
                            mcp,
                            ingredients[index],
                            position["candidates"],
                            selection.ids,
                        )
                    )
            if jobs:
                _, pending = await asyncio.wait(
                    jobs.values(), timeout=max(0, deadline - loop.time())
                )
                for job in pending:
                    job.cancel()
        for index, job in jobs.items():
            if not job.cancelled():
                matches[index] = job.result()

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

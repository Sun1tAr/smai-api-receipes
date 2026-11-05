import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from receipes.api.schemas import IngredientResponse
from receipes.cart.mcp import CART_LINK_TOOL, McpError
from receipes.cart.schemas import (
    CartLinkRequest,
    CartLinkResponse,
    CartRequest,
    CartResponse,
)
from receipes.cart.service import CatalogUnavailable, build_cart
from receipes.db.repositories import MenuRepository
from receipes.errors import ErrorResponse, error_response

router = APIRouter(tags=["recipes"])

_CATALOG_MESSAGE = "VkusVill catalog unavailable"
_CART_LINK_MESSAGE = "VkusVill cart link unavailable"
_CART_LINK_EXAMPLE = {"link": "https://vkusvill.ru/mobile?basket&share=2557567558"}
_INVALID_URL_CHARS = re.compile(
    r'[\s\x00-\x1f\x7f-\x9f\ud800-\udfff\\<>"{}`|^]|%(?![0-9a-fA-F]{2})'
)
_VKUSVILL_AUTHORITY = re.compile(
    r"(?:www\.)?vkusvill\.ru(?::[0-9]+)?", re.IGNORECASE | re.ASCII
)
_NOT_FOUND_MESSAGE = "Recipe not found"
_CART_EXAMPLE = {
    "matches": [
        {
            "ingredient": {"name": "Морковь", "quantity": 150, "unit": "г"},
            "product": {
                "id": 606,
                "name": "Морковь",
                "price": {"current": 53, "currency": "RUB"},
                "unit": "кг",
                "weight": None,
            },
            "q": 0.15,
            "warning": None,
            "error": None,
        },
        {
            "ingredient": {"name": "Мука", "quantity": 100, "unit": "г"},
            "product": None,
            "q": None,
            "warning": None,
            "error": "Товар не найден",
        },
    ],
    "total": 7.95,
    "allMatched": False,
    "unmatchedCount": 1,
}


def _error_doc(
    description: str, code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "example": {
                    "error": {
                        "code": code,
                        "message": message,
                        "details": details or {},
                    }
                }
            }
        },
    }


@router.post(
    "/recipes/cart",
    summary="Подобрать продукты для рецепта (API-14)",
    description=(
        "Возвращает позиции корзины для одного сохранённого рецепта из готового "
        "негостевого меню. Запрос доступен по UUID рецепта без проверки владельца. "
        "Для гостевого или неизвестного рецепта возвращается 404.\n\n"
        "Одна позиция соответствует одному ингредиенту. При отсутствии уверенного "
        "совпадения, несовместимых единицах или частичном сбое каталога позиция "
        "получает `error`, а `product` и `q` равны `null`. Завершённые позиции "
        "сохраняются при истечении общего времени поиска. Цена и вес берутся из "
        "карточки товара. `q` округляется вверх до 0,01 кг либо до целых штук. "
        "При превышении 40 возвращается `q: 40` с предупреждением. "
        "`total` в рублях учитывает только позиции без ошибки."
    ),
    response_model=CartResponse,
    responses={
        200: {
            "description": "Результат сопоставления всех ингредиентов.",
            "content": {"application/json": {"example": _CART_EXAMPLE}},
        },
        400: _error_doc(
            "Тело отсутствует, некорректно или не содержит UUID `recipeId`.",
            "VALIDATION_ERROR",
            "Request validation failed",
            {"fields": [{"loc": ["body", "recipeId"], "type": "uuid_parsing"}]},
        ),
        404: _error_doc(
            "Гостевой рецепт или рецепт из готового негостевого меню не найден.",
            "NOT_FOUND",
            _NOT_FOUND_MESSAGE,
        ),
        500: _error_doc(
            "Внутренняя ошибка, в том числе ошибка при чтении рецепта из БД.",
            "INTERNAL_ERROR",
            "Internal server error",
        ),
        503: _error_doc(
            "Каталог ВкусВилла не настроен или полностью недоступен.",
            "EXTERNAL_SERVICE_ERROR",
            _CATALOG_MESSAGE,
        ),
    },
)
async def get_recipe_cart(
    payload: CartRequest, request: Request
) -> CartResponse | JSONResponse:
    state = request.app.state
    deadline = asyncio.get_running_loop().time() + state.settings.cart_deadline_seconds
    async with asyncio.timeout_at(deadline), state.session_factory() as session:
        recipe = await MenuRepository(session).find_cart_recipe(payload.recipe_id)
        ingredients = (
            [
                IngredientResponse(
                    name=part.ingredient.name, quantity=part.quantity, unit=part.unit
                )
                for part in recipe.ingredients
            ]
            if recipe is not None
            else None
        )
    if ingredients is None:
        return error_response(404, "NOT_FOUND", _NOT_FOUND_MESSAGE)
    if state.vkusvill_mcp is not None:
        try:
            return await build_cart(state.vkusvill_mcp, ingredients, deadline)
        except CatalogUnavailable:
            pass
    return error_response(503, "EXTERNAL_SERVICE_ERROR", _CATALOG_MESSAGE)


def _valid_cart_link(link: object) -> bool:
    if not isinstance(link, str) or _INVALID_URL_CHARS.search(link):
        return False
    try:
        url = urlsplit(link)
        return (
            url.scheme == "https"
            and _VKUSVILL_AUTHORITY.fullmatch(url.netloc) is not None
            and url.port != 0
        )
    except ValueError:
        return False


@router.post(
    "/recipes/cart/link",
    summary="Создать ссылку на корзину ВкусВилла (API-16)",
    description=(
        "Принимает от 1 до 20 позиций с `product.id` и `q` из API-14. "
        "ВкусВилл создаёт одну ссылку для всего списка. Если хотя бы одна "
        "позиция некорректна, запрос целиком завершается ошибкой. "
        "Маршрут доступен без авторизации. Общий дедлайн MCP составляет "
        "не более 9 секунд."
    ),
    response_model=CartLinkResponse,
    responses={
        200: {
            "description": "Ссылка на корзину для всех переданных позиций.",
            "content": {"application/json": {"example": _CART_LINK_EXAMPLE}},
        },
        400: _error_doc(
            "Тело запроса некорректно или ВкусВилл отклонил позиции корзины.",
            "VALIDATION_ERROR",
            "Request validation failed",
        ),
        500: _error_doc(
            "Внутренняя ошибка сервиса рецептов.",
            "INTERNAL_ERROR",
            "Internal server error",
        ),
        503: _error_doc(
            "ВкусВилл недоступен, не настроен или вернул некорректный ответ.",
            "EXTERNAL_SERVICE_ERROR",
            _CART_LINK_MESSAGE,
        ),
    },
)
async def create_recipe_cart_link(
    payload: CartLinkRequest, request: Request
) -> CartLinkResponse | JSONResponse:
    state = request.app.state
    if state.vkusvill_mcp is not None:
        try:
            async with asyncio.timeout(min(state.settings.cart_deadline_seconds, 9)):
                data = await state.vkusvill_mcp.call(
                    CART_LINK_TOOL,
                    {"products": [item.model_dump() for item in payload.items]},
                )
            link = data.get("link")
            if _valid_cart_link(link):
                return CartLinkResponse(link=link)
        except McpError as exc:
            if (
                exc.code == "invalid_input"
                and exc.http_status == 400
                and not exc.unavailable
            ):
                return error_response(
                    400, "VALIDATION_ERROR", "Request validation failed"
                )
        except TimeoutError:
            pass
    return error_response(503, "EXTERNAL_SERVICE_ERROR", _CART_LINK_MESSAGE)


# FastAPI исключает null из примеров при построении OpenAPI.
def restore_cart_example(schema: dict[str, Any]) -> None:
    post = schema["paths"]["/recipes/cart"]["post"]
    post["responses"]["200"]["content"]["application/json"]["example"] = _CART_EXAMPLE

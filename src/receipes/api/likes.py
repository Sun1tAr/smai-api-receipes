from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from receipes.api.dependencies import USER_ID_HEADER, parse_user_id
from receipes.api.schemas import (
    LikedRecipesResponse,
    LikeSuccessResponse,
    NutritionResponse,
    RecipeCardResponse,
)
from receipes.db.repositories import RecipeLikeRepository
from receipes.errors import HTTP_ERRORS, ErrorResponse, error_response

router = APIRouter(prefix="/recipes", tags=["likes"])
LIKE_PATH = "/{recipe_id}/like"
EXAMPLE_RECIPE_ID = "b1e2c3d4-5678-90ab-cdef-1234567890ab"
RECIPE_NOT_FOUND = "Recipe not found"
LIKE_NOT_FOUND = "Recipe like not found"
ALREADY_LIKED = "Recipe already liked"


def _document_error(description: str, code: str, message: str) -> dict[str, Any]:
    return {
        "description": description,
        "model": ErrorResponse,
        "content": {
            "application/json": {
                "example": {"error": {"code": code, "message": message, "details": {}}}
            }
        },
    }


COMMON_ERRORS = {
    401: _document_error(
        "Заголовок `X-User-Id` отсутствует, некорректен или повторён.",
        *HTTP_ERRORS[401],
    ),
    500: _document_error(
        "Внутренняя ошибка сервиса (например, недоступна база).",
        "INTERNAL_ERROR",
        "Internal server error",
    ),
}
SUCCESS = {
    "description": "Операция выполнена.",
    "content": {"application/json": {"example": {"success": True}}},
}
CARD = {
    "recipeId": EXAMPLE_RECIPE_ID,
    "name": "Омлет с томатами",
    "calories": 345.5,
    "nutrition": {"calories": 345.5, "proteins": 20, "fats": 22, "carbohydrates": 17},
    "cookingTime": 25,
}


# Заголовок проверяем вручную, чтобы при его отсутствии вернуть 401.
OWNER_HEADER = {
    "parameters": [
        {
            "name": USER_ID_HEADER,
            "in": "header",
            "required": True,
            "description": (
                "ID из проверенного токена. Gateway передаёт один заголовок "
                "с целым числом от 1 до 2147483647."
            ),
            "schema": {"type": "string"},
            "example": "42",
        }
    ]
}


def _require_user(request: Request) -> int:
    user_id = parse_user_id(request.headers.getlist(USER_ID_HEADER))
    if user_id is None:
        raise HTTPException(status_code=401)
    return user_id


UserId = Annotated[int, Depends(_require_user)]
RecipePath = Annotated[
    str,
    Path(
        description="UUID сохранённого рецепта.",
        examples=[EXAMPLE_RECIPE_ID],
        json_schema_extra={"format": "uuid"},
    ),
]


@router.post(
    LIKE_PATH,
    summary="Поставить лайк рецепту (API-19)",
    description=(
        "Сохраняет лайк для пользователя из доверенного заголовка `X-User-Id`. "
        "Пользователь может лайкнуть любой существующий рецепт один раз."
    ),
    response_model=LikeSuccessResponse,
    openapi_extra=OWNER_HEADER,
    responses={
        200: SUCCESS,
        **COMMON_ERRORS,
        404: _document_error(
            "Рецепт не найден или UUID некорректен.", "NOT_FOUND", RECIPE_NOT_FOUND
        ),
        409: _document_error(
            "Пользователь уже лайкнул рецепт.", "CONFLICT", ALREADY_LIKED
        ),
    },
)
async def like_recipe(request: Request, user_id: UserId, recipe_id: RecipePath) -> Any:
    try:
        parsed_id = UUID(recipe_id)
    except ValueError:
        return error_response(404, "NOT_FOUND", RECIPE_NOT_FOUND)
    async with request.app.state.session_factory() as session, session.begin():
        likes = RecipeLikeRepository(session)
        if not await likes.recipe_exists(parsed_id):
            return error_response(404, "NOT_FOUND", RECIPE_NOT_FOUND)
        if not await likes.add(user_id, parsed_id):
            return error_response(409, "CONFLICT", ALREADY_LIKED)
    return LikeSuccessResponse(success=True)


@router.delete(
    LIKE_PATH,
    summary="Убрать лайк рецепта (API-20)",
    description=(
        "Удаляет лайк пользователя из доверенного заголовка `X-User-Id`. "
        "Другие пользователи сохраняют свои лайки на этот рецепт."
    ),
    response_model=LikeSuccessResponse,
    openapi_extra=OWNER_HEADER,
    responses={
        200: SUCCESS,
        **COMMON_ERRORS,
        404: _document_error(
            "У пользователя нет такого лайка или UUID некорректен.",
            "NOT_FOUND",
            LIKE_NOT_FOUND,
        ),
    },
)
async def unlike_recipe(
    request: Request, user_id: UserId, recipe_id: RecipePath
) -> Any:
    try:
        parsed_id = UUID(recipe_id)
    except ValueError:
        return error_response(404, "NOT_FOUND", LIKE_NOT_FOUND)
    async with request.app.state.session_factory() as session, session.begin():
        if not await RecipeLikeRepository(session).remove(user_id, parsed_id):
            return error_response(404, "NOT_FOUND", LIKE_NOT_FOUND)
    return LikeSuccessResponse(success=True)


@router.get(
    "/liked",
    summary="Список понравившихся рецептов (API-21)",
    description=(
        "Возвращает карточки рецептов, которые лайкнул пользователь из доверенного "
        "заголовка `X-User-Id`. Сначала идут последние лайки, при равном времени "
        "порядок идёт по убыванию UUID рецепта. Если лайков нет, список пуст."
    ),
    response_model=LikedRecipesResponse,
    openapi_extra=OWNER_HEADER,
    responses={
        200: {
            "description": "Список понравившихся рецептов.",
            "content": {
                "application/json": {
                    "examples": {
                        "liked": {
                            "summary": "Есть лайки",
                            "value": {"recipes": [CARD]},
                        },
                        "empty": {"summary": "Лайков нет", "value": {"recipes": []}},
                    }
                }
            },
        },
        **COMMON_ERRORS,
    },
)
async def list_liked_recipes(request: Request, user_id: UserId) -> LikedRecipesResponse:
    async with request.app.state.session_factory() as session:
        rows = await RecipeLikeRepository(session).list_recipes(user_id)
    return LikedRecipesResponse(
        recipes=[
            RecipeCardResponse(
                recipeId=row.id,
                name=row.name,
                calories=row.calories,
                nutrition=NutritionResponse(
                    calories=row.nutrition_calories,
                    proteins=row.proteins,
                    fats=row.fats,
                    carbohydrates=row.carbohydrates,
                ),
                cookingTime=row.cooking_time,
            )
            for row in rows
        ],
    )

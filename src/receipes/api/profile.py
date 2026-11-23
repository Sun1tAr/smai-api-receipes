from datetime import UTC
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from receipes.api.dependencies import (
    OWNER_HEADER,
    USER_ID_HEADER,
    TaskStore,
    UserId,
    get_task_store,
    parse_user_id,
)
from receipes.api.schemas import (
    MenuHistoryItemResponse,
    MenuHistoryRecipeResponse,
    MenuHistoryResponse,
    NutritionResponse,
    SavedParametersResponse,
)
from receipes.db.repositories import MenuRepository
from receipes.errors import ErrorResponse, error_response

router = APIRouter(tags=["profile"])

Store = Annotated[TaskStore, Depends(get_task_store)]

UNAUTHORIZED_BODY = {
    "error": {
        "code": "UNAUTHORIZED",
        "message": "User is not identified",
        "details": {},
    }
}
INTERNAL_BODY = {
    "error": {
        "code": "INTERNAL_ERROR",
        "message": "Internal server error",
        "details": {},
    }
}

# Пустой ответ нужен, пока у пользователя нет ни одной готовой генерации.
SAVED = {"habits": [3, 5], "diet": 4, "allergies": [1, 2], "kitchenTools": [1, 2, 6]}
EMPTY = {"habits": [], "diet": None, "allergies": [], "kitchenTools": []}
EXAMPLES = {
    "saved": {"summary": "Параметры сохранены", "value": SAVED},
    "empty": {"summary": "Готовых генераций ещё нет", "value": EMPTY},
}


@router.get(
    "/profile/parameters",
    summary="Сохранённые параметры пользователя (API-13)",
    description=(
        "Возвращает `habits`, `diet`, `allergies` и `kitchenTools` самой свежей "
        "задачи пользователя в статусе `done` (по времени создания задачи). "
        "Параметры не хранятся отдельно: это параметры последней готовой генерации, "
        "поэтому новая генерация «перезаписывает» результат. `peopleCount` и "
        "`weeklyBudget` не возвращаются. Задачи в статусах `pending`, "
        "`in_progress` и `failed` не учитываются.\n\n"
        "Если готовых задач нет, отвечает 200 с пустыми списками и `diet: null`.\n\n"
        "Сервис внутренний: владельца задаёт заголовок `X-User-Id`, который "
        "выставляет gateway по проверенному токену. Без заголовка или с "
        "некорректным или повторённым значением (одно целое от 1 до 2147483647) "
        "отвечает 401."
    ),
    response_model=SavedParametersResponse,
    responses={
        200: {
            "description": "Сохранённые параметры или пустой ответ.",
            "content": {"application/json": {"examples": EXAMPLES}},
        },
        401: {
            "model": ErrorResponse,
            "description": "`X-User-Id` отсутствует или некорректен.",
            "content": {"application/json": {"example": UNAUTHORIZED_BODY}},
        },
        500: {
            "model": ErrorResponse,
            "description": "Внутренняя ошибка сервиса (например, недоступна база).",
            "content": {"application/json": {"example": INTERNAL_BODY}},
        },
    },
)
async def get_saved_parameters(
    request: Request,
    store: Store,
    _x_user_id: Annotated[
        str | None,
        Header(
            alias=USER_ID_HEADER,
            description=(
                "ID пользователя из токена, целое от 1 до 2147483647, заголовок один."
            ),
            examples=["42"],
        ),
    ] = None,
) -> Any:
    user_id = parse_user_id(request.headers.getlist(USER_ID_HEADER))
    if user_id is None:
        return error_response(401, "UNAUTHORIZED", "User is not identified")
    row = await store.last_done_parameters(user_id)
    if row is None:
        return SavedParametersResponse(
            habits=[], diet=None, allergies=[], kitchenTools=[]
        )
    return SavedParametersResponse(
        habits=sorted(item.habit_id for item in row.habits),
        diet=row.diet_id,
        allergies=sorted(item.allergen_id for item in row.allergies),
        kitchenTools=sorted(item.kitchen_tool_id for item in row.kitchen_tools),
    )


HISTORY_EXAMPLES = {
    "history": {
        "summary": "Есть готовые меню",
        "value": {
            "menus": [
                {
                    "menuId": "d94d77a7-90f6-46f5-ac10-e9b50545714c",
                    "createdAt": "2026-11-01T12:00:00Z",
                    "recipes": [
                        {
                            "recipeId": "b1e2c3d4-5678-90ab-cdef-1234567890ab",
                            "name": "Омлет с томатами",
                            "calories": 345.5,
                            "nutrition": {
                                "calories": 340,
                                "proteins": 20.5,
                                "fats": 24,
                                "carbohydrates": 10.5,
                            },
                            "cookingTime": 25,
                        }
                    ],
                }
            ]
        },
    },
    "empty": {"summary": "Готовых меню нет", "value": {"menus": []}},
}


@router.get(
    "/profile/menus",
    summary="История меню пользователя (API-22)",
    description=(
        "Возвращает готовые меню пользователя из доверенного заголовка "
        "`X-User-Id`. Меню отсортированы по убыванию времени создания задачи, "
        "при равном времени — по убыванию UUID задачи. В составе меню "
        "отображаются текущие рецепты с учётом замен. При отсутствии готовых "
        "меню список пуст. Рецепты содержат сохранённые КБЖУ одной порции "
        "в `nutrition`; основная калорийность — `nutrition.calories`."
    ),
    response_model=MenuHistoryResponse,
    openapi_extra=OWNER_HEADER,
    responses={
        200: {
            "description": "Список готовых меню.",
            "content": {"application/json": {"examples": HISTORY_EXAMPLES}},
        },
        401: {
            "model": ErrorResponse,
            "description": "`X-User-Id` отсутствует, некорректен или повторён.",
            "content": {"application/json": {"example": UNAUTHORIZED_BODY}},
        },
        500: {
            "model": ErrorResponse,
            "description": "Внутренняя ошибка сервиса (например, недоступна база).",
            "content": {"application/json": {"example": INTERNAL_BODY}},
        },
    },
)
async def list_menu_history(request: Request, user_id: UserId) -> MenuHistoryResponse:
    async with request.app.state.session_factory() as session:
        rows = await MenuRepository(session).list_history(user_id)
    menus: dict[UUID, MenuHistoryItemResponse] = {}
    for row in rows:
        if row.task_id not in menus:
            menus[row.task_id] = MenuHistoryItemResponse(
                menuId=row.menu_id,
                createdAt=row.created_at.astimezone(UTC),
                recipes=[],
            )
        if row.recipe_id is not None:
            menus[row.task_id].recipes.append(
                MenuHistoryRecipeResponse(
                    recipeId=row.recipe_id,
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
            )
    return MenuHistoryResponse(menus=list(menus.values()))


# FastAPI вырезает null из примеров (exclude_none): примеры 200 подставляются целиком,
# чтобы вернуть `diet: null`.
def restore_profile_examples(schema: dict[str, Any]) -> None:
    operation = schema["paths"]["/profile/parameters"]["get"]
    content = operation["responses"]["200"]["content"]["application/json"]
    content["examples"] = EXAMPLES

import json
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import jsonschema
import pytest
from fakes import unavailable_db_settings

from receipes.api.dependencies import get_dictionaries, get_task_store
from receipes.db.models import (
    GenerationParameters,
    GenerationTask,
    Ingredient,
    Instruction,
    Menu,
    MenuRecipe,
    Nutrition,
    Recipe,
    RecipeIngredient,
)
from receipes.dictionaries import Dictionaries
from receipes.domain.params import GenerationParams
from receipes.domain.status import TaskStatus


class FakeStore:
    def __init__(self) -> None:
        self.created: list[tuple[int, Decimal]] = []
        self.params: list[GenerationParams] = []
        self.dictionaries: list[Dictionaries | None] = []
        self.tasks: dict[UUID, GenerationTask] = {}
        self.fail = False
        self.user_ids: list[int | None] = []
        self.saved: dict[int, GenerationParameters] = {}

    async def create(
        self,
        params: GenerationParams,
        dictionaries: Dictionaries | None = None,
        user_id: int | None = None,
    ) -> UUID:
        if self.fail:
            raise ConnectionError("SELECT secret FROM users")
        self.user_ids.append(user_id)
        self.created.append((params.people_count, params.weekly_budget))
        self.params.append(params)
        self.dictionaries.append(dictionaries)
        return UUID("b1e2c3d4-5678-90ab-cdef-1234567890ab")

    async def last_done_parameters(self, user_id: int) -> GenerationParameters | None:
        if self.fail:
            raise ConnectionError("SELECT secret FROM users")
        return self.saved.get(user_id)

    async def get(self, task_id: UUID) -> GenerationTask | None:
        if self.fail:
            raise ConnectionError("SELECT secret FROM users")
        return self.tasks.get(task_id)


# Справочники MS-2 с запасом ID: тесты этого файла не про проверку ID (она в
# test_api_validation.py).
class FakeDictionaries:
    async def fetch(self) -> Dictionaries:
        ids = {i: str(i) for i in range(1, 10)}
        return Dictionaries(
            habits={**ids, -(2**31): "min"},
            diets=ids,
            allergies=ids,
            kitchen_tools={**ids, 2**31 - 1: "max"},
        )


def make_recipe(name: str, quantity: str = "35.00") -> MenuRecipe:
    recipe = Recipe(
        id=uuid4(),
        name=name,
        calories=Decimal("350.00"),
        cooking_time=Decimal("12.50"),
        photo="https://must-not-leak.example/x.jpg",
        nutrition=Nutrition(
            calories=Decimal("349.99"),
            proteins=Decimal("20.00"),
            fats=Decimal("10.50"),
            carbohydrates=Decimal("40.00"),
        ),
        ingredients=[
            RecipeIngredient(
                ingredient=Ingredient(name="Мука"),
                quantity=Decimal(quantity),
                unit="г",
            )
        ],
        instructions=[
            Instruction(text="Первый шаг", position=1),
            Instruction(text="Второй шаг", position=2),
        ],
    )
    return MenuRecipe(recipe=recipe)


def make_task(status: TaskStatus, **fields: object) -> GenerationTask:
    return GenerationTask(
        task_id=uuid4(),
        status=status.value,
        parameters=GenerationParameters(people_count=4, weekly_budget=Decimal("5000")),
        **fields,
    )


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
async def client(store: FakeStore):
    from receipes.main import create_app

    app = create_app(unavailable_db_settings())
    app.dependency_overrides[get_task_store] = lambda: store
    app.dependency_overrides[get_dictionaries] = FakeDictionaries
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield client
    await app.state.engine.dispose()
    app.state.tracer_provider.shutdown()


async def test_post_creates_task_and_returns_202_with_task_id(client, store) -> None:
    response = await client.post(
        "/generate-menu",
        json={"peopleCount": 4, "weeklyBudget": 5000, "habits": [], "kitchenTools": []},
    )
    assert response.status_code == 202
    assert response.json() == {"task_id": "b1e2c3d4-5678-90ab-cdef-1234567890ab"}
    assert store.created == [(4, Decimal("5000.0"))]
    assert response.headers["x-request-id"]


@pytest.mark.parametrize(
    ("people", "budget", "expected"),
    [
        (1, 0.01, Decimal("0.01")),
        (20, 1_000_000, Decimal("1000000.0")),
        (4, 5000.5, Decimal("5000.5")),
        (4, 1234.56, Decimal("1234.56")),
    ],
)
async def test_boundary_values_are_accepted(
    client, store, people: int, budget: float, expected: Decimal
) -> None:
    response = await client.post(
        "/generate-menu",
        json={
            "peopleCount": people,
            "weeklyBudget": budget,
            "habits": [],
            "kitchenTools": [],
        },
    )
    assert response.status_code == 202
    assert store.created == [(people, expected)]


async def test_fields_of_version_2_are_passed_to_the_store(client, store) -> None:
    response = await client.post(
        "/generate-menu",
        json={
            "peopleCount": 2,
            "weeklyBudget": 100,
            "habits": [3, 5],
            "diet": 4,
            "allergies": [1],
            "kitchenTools": [1, 2],
            "unknown": {"x": 1},
        },
    )
    assert response.status_code == 202
    assert store.params == [
        GenerationParams(2, Decimal("100.0"), (3, 5), 4, (1,), (1, 2))
    ]


VALID_BODY = (
    b'{"peopleCount": 4, "weeklyBudget": 5000, "habits": [], "kitchenTools": []}'
)
BAD_BODIES = [
    pytest.param({}, id="empty-object"),
    pytest.param(
        {"weeklyBudget": 5000, "habits": [], "kitchenTools": []}, id="no-people"
    ),
    pytest.param({"peopleCount": 4}, id="no-budget"),
    pytest.param(
        {"peopleCount": 0, "weeklyBudget": 5000, "habits": [], "kitchenTools": []},
        id="people-0",
    ),
    pytest.param(
        {"peopleCount": 21, "weeklyBudget": 5000, "habits": [], "kitchenTools": []},
        id="people-21",
    ),
    pytest.param(
        {"peopleCount": -1, "weeklyBudget": 5000, "habits": [], "kitchenTools": []},
        id="people-neg",
    ),
    pytest.param(
        {"peopleCount": "4", "weeklyBudget": 5000, "habits": [], "kitchenTools": []},
        id="people-str",
    ),
    pytest.param(
        {"peopleCount": True, "weeklyBudget": 5000, "habits": [], "kitchenTools": []},
        id="people-bool",
    ),
    pytest.param(
        {"peopleCount": 4.0, "weeklyBudget": 5000, "habits": [], "kitchenTools": []},
        id="people-float",
    ),
    pytest.param(
        {"peopleCount": None, "weeklyBudget": 5000, "habits": [], "kitchenTools": []},
        id="people-null",
    ),
    pytest.param(
        {"peopleCount": 4, "weeklyBudget": 0, "habits": [], "kitchenTools": []},
        id="budget-0",
    ),
    pytest.param(
        {"peopleCount": 4, "weeklyBudget": -1, "habits": [], "kitchenTools": []},
        id="budget-neg",
    ),
    pytest.param(
        {"peopleCount": 4, "weeklyBudget": 0.001, "habits": [], "kitchenTools": []},
        id="budget-3-places",
    ),
    pytest.param(
        {"peopleCount": 4, "weeklyBudget": 12.345, "habits": [], "kitchenTools": []},
        id="budget-3-places-b",
    ),
    pytest.param(
        {
            "peopleCount": 4,
            "weeklyBudget": 1000000.01,
            "habits": [],
            "kitchenTools": [],
        },
        id="budget-over",
    ),
    pytest.param(
        {"peopleCount": 4, "weeklyBudget": "5000", "habits": [], "kitchenTools": []},
        id="budget-str",
    ),
    pytest.param(
        {"peopleCount": 4, "weeklyBudget": True, "habits": [], "kitchenTools": []},
        id="budget-bool",
    ),
    pytest.param(
        {"peopleCount": 4, "weeklyBudget": None, "habits": [], "kitchenTools": []},
        id="budget-null",
    ),
    pytest.param([1, 2], id="array"),
    pytest.param("text", id="string"),
    pytest.param(None, id="null"),
]


@pytest.mark.parametrize("body", BAD_BODIES)
async def test_invalid_body_is_400_in_service_format(client, store, body) -> None:
    response = await client.post("/generate-menu", json=body)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "Request validation failed"
    assert error["details"]["fields"]
    assert all(
        isinstance(part, str)
        for field in error["details"]["fields"]
        for part in field["loc"]
    )
    assert store.created == []


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_nan_and_infinity_are_rejected(client, store, literal: str) -> None:
    response = await client.post(
        "/generate-menu",
        content=(
            f'{{"peopleCount": 4, "weeklyBudget": {literal}, '
            '"habits": [], "kitchenTools": []}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert store.created == []


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"", "application/json"),
        (b"{broken", "application/json"),
        (
            VALID_BODY,
            "text/plain",
        ),
        (
            VALID_BODY,
            "application/x-www-form-urlencoded",
        ),
    ],
)
async def test_empty_broken_or_non_json_body_is_400(
    client, store, content: bytes, content_type: str
) -> None:
    response = await client.post(
        "/generate-menu", content=content, headers={"Content-Type": content_type}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert store.created == []


async def test_post_database_failure_is_500_without_details(client, store) -> None:
    store.fail = True
    response = await client.post(
        "/generate-menu",
        json={"peopleCount": 4, "weeklyBudget": 5000, "habits": [], "kitchenTools": []},
        headers={"X-Request-ID": "rid-api1"},
    )
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
            "details": {},
        }
    }
    assert response.headers["x-request-id"] == "rid-api1"
    assert "secret" not in response.text


@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_get_waiting_statuses_have_no_menu_and_no_error(
    client, store, status: str
) -> None:
    task = make_task(TaskStatus(status))
    store.tasks[task.task_id] = task
    response = await client.get(f"/generate-menu/{task.task_id}")
    assert response.status_code == 200
    assert response.json() == {"status": status, "menu": None, "error": None}


async def test_get_failed_returns_error_text(client, store) -> None:
    task = make_task(TaskStatus.FAILED, error="Neurocore timeout")
    store.tasks[task.task_id] = task
    response = await client.get(f"/generate-menu/{task.task_id}")
    assert response.json() == {
        "status": "failed",
        "menu": None,
        "error": "Neurocore timeout",
    }


async def test_get_done_returns_menu_with_all_fields(client, store) -> None:
    menu = Menu(menu_recipes=[make_recipe(f"Блюдо {i}") for i in range(3)])
    task = make_task(TaskStatus.DONE, menu=menu)
    store.tasks[task.task_id] = task
    response = await client.get(f"/generate-menu/{task.task_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done" and body["error"] is None
    assert len(body["menu"]["recipes"]) == 3
    expected_ids = {link.recipe.name: str(link.recipe.id) for link in menu.menu_recipes}
    for result in (body, (await client.get(f"/generate-menu/{task.task_id}")).json()):
        actual_ids = {
            item["name"]: item["recipeId"] for item in result["menu"]["recipes"]
        }
        assert actual_ids == expected_ids
        assert all(str(UUID(value)) == value for value in actual_ids.values())
    recipe = body["menu"]["recipes"][0]
    assert recipe == {
        "recipeId": str(menu.menu_recipes[0].recipe.id),
        "name": "Блюдо 0",
        "ingredients": [{"name": "Мука", "quantity": 35, "unit": "г"}],
        "calories": 350,
        "nutrition": {
            "calories": 349.99,
            "proteins": 20,
            "fats": 10.5,
            "carbohydrates": 40,
        },
        "cookingTime": 12.5,
        "instructions": ["Первый шаг", "Второй шаг"],
        "photo": None,
    }


async def test_whole_numbers_are_serialized_without_fraction(client, store) -> None:
    menu = Menu(menu_recipes=[make_recipe("Блюдо", "35.00")])
    task = make_task(TaskStatus.DONE, menu=menu)
    store.tasks[task.task_id] = task
    text = (await client.get(f"/generate-menu/{task.task_id}")).text
    assert '"quantity":35,' in text and "35.0" not in text
    assert '"cookingTime":12.5' in text and '"calories":350,' in text


async def test_done_task_without_menu_data_is_500(client, store) -> None:
    task = make_task(TaskStatus.DONE, menu=Menu(menu_recipes=[]))
    store.tasks[task.task_id] = task
    response = await client.get(f"/generate-menu/{task.task_id}")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.parametrize("task_id", [str(uuid4()), "not-a-uuid", "123", "b1e2c3d4"])
async def test_unknown_or_malformed_task_id_is_404(client, task_id: str) -> None:
    response = await client.get(f"/generate-menu/{task_id}")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Generation task not found",
            "details": {},
        }
    }


async def test_get_database_failure_is_500(client, store) -> None:
    store.fail = True
    response = await client.get(f"/generate-menu/{uuid4()}")
    assert response.status_code == 500
    assert "secret" not in response.text


async def test_get_accepts_uppercase_uuid(client, store) -> None:
    task = make_task(TaskStatus.PENDING)
    store.tasks[task.task_id] = task
    response = await client.get(f"/generate-menu/{str(task.task_id).upper()}")
    assert response.status_code == 200


async def test_openapi_examples_match_the_response_schema(client) -> None:
    document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    get = document["paths"]["/generate-menu/{task_id}"]["get"]
    examples = get["responses"]["200"]["content"]["application/json"]["examples"]
    assert set(examples) == {"pending", "in_progress", "done", "failed"}
    schema = {**schemas["TaskResultResponse"], "components": {"schemas": schemas}}
    for example in examples.values():
        jsonschema.validate(example["value"], schema)
        assert set(example["value"]) == {"status", "menu", "error"}
    done = examples["done"]["value"]
    assert len(done["menu"]["recipes"]) >= 3
    assert all(recipe["photo"] is None for recipe in done["menu"]["recipes"])
    assert json.dumps(done)  # сериализуется без Decimal


async def test_snake_case_fields_are_not_accepted(client, store) -> None:
    response = await client.post(
        "/generate-menu", json={"people_count": 4, "weekly_budget": 5000}
    )
    assert response.status_code == 400
    locs = {
        tuple(field["loc"]) for field in response.json()["error"]["details"]["fields"]
    }
    assert locs == {
        ("body", "peopleCount"),
        ("body", "weeklyBudget"),
        ("body", "habits"),
        ("body", "kitchenTools"),
    }
    assert store.created == []


@pytest.mark.parametrize("body", ["[" * 100_000, '{"a":' * 50_000])
async def test_unparsable_deeply_nested_json_is_validation_error(
    client, store, body: str
) -> None:
    response = await client.post(
        "/generate-menu", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "details": {"fields": [{"loc": ["body"], "type": "body_invalid"}]},
        }
    }
    assert store.created == []


async def test_task_id_path_parameter_is_documented(client) -> None:
    document = (await client.get("/openapi.json")).json()
    (parameter,) = document["paths"]["/generate-menu/{task_id}"]["get"]["parameters"]
    assert parameter["name"] == "task_id" and parameter["in"] == "path"
    assert "UUID" in parameter["description"]


BASE = {"peopleCount": 4, "weeklyBudget": 5000}


async def test_empty_choices_and_null_diet_are_accepted(client, store) -> None:
    response = await client.post(
        "/generate-menu",
        json={**BASE, "habits": [], "diet": None, "allergies": [], "kitchenTools": []},
    )
    assert response.status_code == 202
    assert store.params == [GenerationParams(4, Decimal("5000.0"))]


async def test_optional_fields_default_to_empty(client, store) -> None:
    response = await client.post(
        "/generate-menu", json={**BASE, "habits": [2], "kitchenTools": [1]}
    )
    assert response.status_code == 202
    assert store.params[0].diet is None and store.params[0].allergies == ()


async def test_duplicates_are_stored_once_in_id_order(client, store) -> None:
    response = await client.post(
        "/generate-menu",
        json={**BASE, "habits": [5, 3, 5], "kitchenTools": [2, 2], "allergies": [7, 1]},
    )
    assert response.status_code == 202
    params = store.params[0]
    assert (params.habits, params.kitchen_tools, params.allergies) == (
        (3, 5),
        (2,),
        (1, 7),
    )


@pytest.mark.parametrize(
    ("extra", "loc"),
    [
        pytest.param({"kitchenTools": []}, ["body", "habits"], id="habits-missing"),
        pytest.param({"habits": []}, ["body", "kitchenTools"], id="tools-missing"),
        pytest.param(
            {"habits": [True], "kitchenTools": []}, ["body", "habits", "0"], id="bool"
        ),
        pytest.param(
            {"habits": ["3"], "kitchenTools": []}, ["body", "habits", "0"], id="str"
        ),
        pytest.param(
            {"habits": [3.0], "kitchenTools": []}, ["body", "habits", "0"], id="float"
        ),
        pytest.param(
            {"habits": [None], "kitchenTools": []}, ["body", "habits", "0"], id="null"
        ),
        pytest.param(
            {"habits": 3, "kitchenTools": []}, ["body", "habits"], id="scalar"
        ),
        pytest.param(
            {"habits": [], "kitchenTools": [], "diet": "1"}, ["body", "diet"], id="diet"
        ),
        pytest.param(
            {"habits": [], "kitchenTools": [], "allergies": None},
            ["body", "allergies"],
            id="allergies-null",
        ),
        pytest.param(
            {"habits": [2**31], "kitchenTools": []},
            ["body", "habits", "0"],
            id="over-int4",
        ),
        pytest.param(
            {"habits": [], "kitchenTools": [-(2**31) - 1]},
            ["body", "kitchenTools", "0"],
            id="under-int4",
        ),
    ],
)
async def test_invalid_v2_fields_return_400(client, store, extra, loc) -> None:
    response = await client.post("/generate-menu", json={**BASE, **extra})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert [field["loc"] for field in error["details"]["fields"]] == [loc]
    assert store.params == []


async def test_int4_bounds_are_accepted(client, store) -> None:
    response = await client.post(
        "/generate-menu",
        json={**BASE, "habits": [-(2**31)], "kitchenTools": [2**31 - 1]},
    )
    assert response.status_code == 202

import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fakes import unavailable_db_settings

from receipes.db.models import Ingredient, Recipe, RecipeIngredient
from receipes.db.repositories import MenuRepository
from receipes.main import create_app

FIXTURES = Path(__file__).parents[1] / "fixtures"
CATALOG = {
    case["ingredient"]: case
    for case in json.loads(
        (FIXTURES / "cart" / "catalog.json").read_text(), parse_float=Decimal
    )["cases"]
}
SEARCH = "vkusvill_products_search"
DETAILS = "vkusvill_product_details"


def ingredient(name: str, quantity: str = "1000", unit: str = "г") -> RecipeIngredient:
    return RecipeIngredient(
        ingredient=Ingredient(name=name), quantity=Decimal(quantity), unit=unit
    )


class Catalog:
    def __init__(self, cases=CATALOG) -> None:
        self.cases = cases
        self.lookups = []
        self.session_open = False

    async def call(self, tool, arguments):
        assert not self.session_open
        self.lookups.append((tool, arguments))
        if tool == SEARCH:
            case = self.cases[arguments["q"].lower()]
            assert arguments["vvonly"] == arguments["page"] == 1
            assert arguments["sort"] == "popularity"
            assert arguments["mode"] == "custom"
            assert set(arguments["fields"]) == {"id", "name", "price", "unit", "weight"}
            if "category_id" in arguments:
                assert arguments["category_id"] == case["category_id"]
                return {"items": case["items"]}
            return {"facets": case["facets"], "items": []}
        assert tool == DETAILS
        return next(
            item
            for case in self.cases.values()
            for item in case["details"]
            if item["id"] == arguments["id"]
        )


@pytest.fixture
def setup_cart(monkeypatch):
    def setup(
        ingredients, catalog=None, *, recipe_exists=True, db_error=None, **settings
    ):
        app = create_app(unavailable_db_settings(**settings))
        recipe_id = uuid4()

        @asynccontextmanager
        async def session_factory():
            if catalog is not None:
                catalog.session_open = True
            try:
                yield object()
            finally:
                if catalog is not None:
                    catalog.session_open = False

        async def find_cart_recipe(self, requested_id):
            assert catalog is None or catalog.session_open
            if db_error:
                raise db_error
            if recipe_exists and requested_id == recipe_id:
                return Recipe(id=recipe_id, ingredients=ingredients)
            return None

        app.state.session_factory = session_factory
        app.state.vkusvill_mcp = catalog
        monkeypatch.setattr(
            MenuRepository, "find_cart_recipe", find_cart_recipe, raising=False
        )
        return app, str(recipe_id)

    return setup


async def post_cart(app, recipe_id):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/recipes/cart", json={"recipeId": recipe_id})


async def test_catalog_captures_choose_only_supported_matches(setup_cart):
    catalog = Catalog()
    app, recipe_id = setup_cart([ingredient(name) for name in CATALOG], catalog)
    response = await post_cart(app, recipe_id)
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 537
    assert result["allMatched"] is False
    assert result["unmatchedCount"] == 5
    assert [row["ingredient"]["name"] for row in result["matches"]] == list(CATALOG)
    for row, case in zip(result["matches"], CATALOG.values(), strict=True):
        assert row["ingredient"] == {
            "name": case["ingredient"],
            "quantity": 1000,
            "unit": "г",
        }
        assert set(row) == {"ingredient", "product", "q", "warning", "error"}
        if case["expected_id"] is None:
            assert row["product"] is None and row["q"] is None and row["error"]
        else:
            assert row["product"]["id"] == case["expected_id"]
            assert "&nbsp;" not in row["product"]["name"]
            assert row["warning"] is None and row["error"] is None


@pytest.mark.parametrize(
    ("amount", "unit", "q", "warning"),
    [
        ("1", "мл", 1, None),
        ("1.4", "кг", 1, None),
        ("1.41", "кг", 2, None),
        ("1.5", "шт.", 2, None),
        ("100", "шт", 40, "количество ограничено 40"),
    ],
)
async def test_real_pack_weight_uses_positive_gram_tolerance(
    setup_cart, amount, unit, q, warning
):
    wire = json.loads((FIXTURES / "mcp" / "details-milk.json").read_text())
    details = json.loads(wire["result"]["content"][0]["text"], parse_float=Decimal)[
        "data"
    ]
    case = {
        **CATALOG["молоко"],
        "items": [details],
        "details": [details],
    }
    app, recipe_id = setup_cart(
        [ingredient("Молоко", amount, unit)], Catalog({"молоко": case})
    )
    response = await post_cart(app, recipe_id)
    assert response.status_code == 200
    result = response.json()
    assert result["allMatched"] is True and result["unmatchedCount"] == 0
    assert result["total"] == 167 * q
    assert result["matches"][0]["q"] == q
    assert result["matches"][0]["warning"] == warning


@pytest.mark.parametrize(
    "update",
    [
        {"available": False},
        {"available": None},
        {"id": 2},
        {"id": True},
        {"name": "Молоко овсяное"},
        {"unit": "л"},
        {"weight": None},
        {"weight": {"value": 900, "unit": "г"}},
        {"price": {"current": True, "currency": "RUB"}},
        {"price": {"current": "100", "currency": "RUB"}},
        {"price": {"current": -1, "currency": "RUB"}},
        {"price": {"current": float("inf"), "currency": "RUB"}},
    ],
)
async def test_details_recheck_does_not_sell_invalid_product(setup_cart, update):
    case = CATALOG["молоко"]
    details = {**case["details"][0], **update}

    class ChangedDetails(Catalog):
        async def call(self, tool, arguments):
            if tool == DETAILS:
                return details
            return await super().call(tool, arguments)

    app, recipe_id = setup_cart([ingredient("молоко")], ChangedDetails())
    response = await post_cart(app, recipe_id)
    assert response.status_code == 200
    result = response.json()
    assert result["matches"][0]["product"] is None
    assert result["matches"][0]["q"] is None
    assert result["matches"][0]["error"]
    assert result["total"] == 0 and result["unmatchedCount"] == 1


@pytest.mark.parametrize(
    "update", [{"available": False}, {"name": "Молоко овсяное"}, {"weight": None}]
)
async def test_uses_next_strict_candidate_after_unsuitable_card(setup_cart, update):
    case = CATALOG["молоко"]
    first, second = case["items"][:2]
    first_card = case["details"][0]
    catalog = Catalog(
        {
            "молоко": {
                **case,
                "items": [first, second],
                "details": [{**first_card, **update}, {**first_card, **second}],
            }
        }
    )
    app, recipe_id = setup_cart([ingredient("молоко")], catalog)

    response = await post_cart(app, recipe_id)

    assert response.status_code == 200
    row = response.json()["matches"][0]
    assert row["error"] is None
    assert row["product"]["id"] == 173
    assert row["q"] == 1


async def test_incompatible_error_survives_later_invalid_card(setup_cart):
    case = CATALOG["молоко"]
    first, second = case["items"][:2]
    first_card = case["details"][0]
    catalog = Catalog(
        {
            "молоко": {
                **case,
                "items": [first, second],
                "details": [
                    {**first_card, "weight": None},
                    {**first_card, **second, "available": False},
                ],
            }
        }
    )
    app, recipe_id = setup_cart([ingredient("молоко")], catalog)

    response = await post_cart(app, recipe_id)

    assert response.status_code == 200
    assert response.json()["matches"][0]["error"] == "несовместимые единицы"
    assert catalog.lookups[-1] == (DETAILS, {"id": 173})


async def test_uses_card_price_instead_of_search_price(setup_cart):
    class ChangedPrice(Catalog):
        async def call(self, tool, arguments):
            data = await super().call(tool, arguments)
            if tool == DETAILS:
                return {**data, "price": {"current": Decimal("0.1"), "currency": "RUB"}}
            return data

    app, recipe_id = setup_cart(
        [ingredient("морковь", "11"), ingredient("морковь", "10")], ChangedPrice()
    )
    response = await post_cart(app, recipe_id)
    assert response.status_code == 200
    result = response.json()
    assert [row["q"] for row in result["matches"]] == [0.02, 0.01]
    assert result["total"] == 0.003


@pytest.mark.parametrize(
    ("recipe_exists", "bad_id", "db_error", "status", "code"),
    [
        (True, "bad", None, 400, "VALIDATION_ERROR"),
        (False, None, None, 404, "NOT_FOUND"),
        (True, None, None, 503, "EXTERNAL_SERVICE_ERROR"),
        (True, None, TimeoutError(), 500, "INTERNAL_ERROR"),
    ],
)
async def test_request_errors_keep_service_envelope(
    setup_cart, recipe_exists, bad_id, db_error, status, code
):
    app, recipe_id = setup_cart(
        [ingredient("морковь")], recipe_exists=recipe_exists, db_error=db_error
    )
    response = await post_cart(app, bad_id or recipe_id)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert set(response.json()["error"]) == {"code", "message", "details"}


@pytest.mark.parametrize(("first_search_works", "status"), [(False, 503), (True, 200)])
async def test_only_complete_infrastructure_failure_is_503(
    setup_cart, first_search_works, status
):
    from receipes.cart.mcp import McpError

    class Unavailable(Catalog):
        async def call(self, tool, arguments):
            if first_search_works and "category_id" not in arguments:
                return await super().call(tool, arguments)
            raise McpError("catalog down", unavailable=True)

    app, recipe_id = setup_cart(
        [ingredient("морковь"), ingredient("неизвестно", unit="щепотка")], Unavailable()
    )
    response = await post_cart(app, recipe_id)
    assert response.status_code == status
    if status == 200:
        result = response.json()
        assert result["unmatchedCount"] == 2
        assert result["matches"][1]["error"] == "единица не конвертируется"


async def test_deadline_cancels_slow_work_and_keeps_completed_rows(setup_cart):
    cancelled = asyncio.Event()

    class SlowCatalog(Catalog):
        async def call(self, tool, arguments):
            if arguments.get("q") == "мука":
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return await super().call(tool, arguments)

    app, recipe_id = setup_cart(
        [ingredient("мука"), ingredient("морковь")],
        SlowCatalog(),
        cart_deadline_seconds=0.05,
    )
    response = await asyncio.wait_for(post_cart(app, recipe_id), 1)
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 53 and result["unmatchedCount"] == 1
    assert result["matches"][0]["error"]
    assert result["matches"][1]["product"]["id"] == 606
    assert cancelled.is_set()


@pytest.mark.parametrize(("second_id", "product_id"), [(287, 606), (288, None)])
async def test_only_one_distinct_matching_facet_id_is_allowed(
    setup_cart, second_id, product_id
):
    case = CATALOG["морковь"]
    selected = case["facets"]["categories"][0]
    categories = [
        {**selected, "subcategory_name": "Овощи"},
        {**selected, "category_id": second_id, "subcategory_name": "Корнеплоды"},
    ]
    app, recipe_id = setup_cart(
        [ingredient("морковь")],
        Catalog({"морковь": {**case, "facets": {"categories": categories}}}),
    )
    response = await post_cart(app, recipe_id)
    assert response.status_code == 200
    row = response.json()["matches"][0]
    assert (row["product"]["id"] if row["product"] is not None else None) == product_id

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
from receipes.generation.ollama import OllamaClient
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
            return {"facets": case["facets"], "items": case["items"]}
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
        ingredients,
        catalog=None,
        *,
        recipe_exists=True,
        db_error=None,
        model_response=None,
        model_handler=None,
        **settings,
    ):
        app = create_app(unavailable_db_settings(**settings))
        recipe_id = uuid4()

        def model(request):
            body = json.loads(request.content)
            positions = json.loads(body["prompt"].split("\nDATA\n", 1)[1])["positions"]
            rows = []
            for position in positions:
                case = catalog.cases[position["ingredient"].lower()]
                ids = case.get("selected_ids", [case["expected_id"]])
                rows.append(
                    {"index": position["index"], "ids": ids if ids != [None] else None}
                )
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "response": json.dumps(
                        model_response
                        if model_response is not None
                        else {"matches": rows}
                    ),
                },
            )

        app.state.ollama = OllamaClient(
            app.state.settings,
            OllamaClient.build_http_client(
                app.state.settings, httpx.MockTransport(model_handler or model)
            ),
            app.state.ollama._metrics,
            app.state.ollama._tracer,
        )

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
        "selected_ids": [details["id"]],
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
                "selected_ids": [first["id"], second["id"]],
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
                "selected_ids": [first["id"], second["id"]],
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
            if first_search_works and tool == SEARCH:
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


@pytest.mark.parametrize(("second_id", "product_id"), [(287, 606), (288, 606)])
async def test_facets_do_not_discard_real_candidates(setup_cart, second_id, product_id):
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


@pytest.mark.parametrize(
    "facets",
    [
        {"categories": []},
        {
            "categories": [
                {"category_id": 624, "category_name": "Овощи", "subcategory_name": ""}
            ]
        },
    ],
)
async def test_first_search_items_and_semantic_synonyms_reach_model(setup_cart, facets):
    onion = {
        "id": 605,
        "name": "Лук репчатый",
        "unit": "кг",
        "price": {"current": 55, "currency": "RUB"},
    }
    cumin = {
        "id": 915,
        "name": "Зира молотая",
        "unit": "шт",
        "weight": {"value": 0.02, "unit": "кг"},
        "price": {"current": 100, "currency": "RUB"},
    }
    cases = {
        name: {
            "ingredient": name,
            "items": [item],
            "details": [item],
            "facets": facets,
            "expected_id": item["id"],
        }
        for name, item in [("лук репчатый", onion), ("кумин молотый", cumin)]
    }
    app, recipe_id = setup_cart(
        [ingredient("лук репчатый", "100"), ingredient("кумин молотый", "2")],
        Catalog(cases),
    )
    result = (await post_cart(app, recipe_id)).json()
    assert [
        row["product"]["id"] if row["product"] else None for row in result["matches"]
    ] == [605, 915]
    assert result["total"] == 105.5


@pytest.mark.parametrize(
    "bad", [None, [], [True], [999999], [606, 606], [36296], ["606"]]
)
async def test_bad_selection_never_falls_back_to_first_product(setup_cart, bad):
    app, recipe_id = setup_cart(
        [ingredient("морковь")],
        Catalog(),
        model_response={"matches": [{"index": 0, "ids": bad}]},
    )
    result = (await post_cart(app, recipe_id)).json()
    assert result["matches"][0]["product"] is None
    assert result["matches"][0]["error"]
    assert result["total"] == 0


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"index": True, "ids": [606]}],
        [{"index": 0, "ids": [606]}, {"index": 0, "ids": [606]}],
    ],
)
async def test_omitted_or_duplicated_position_is_safe(setup_cart, rows):
    app, recipe_id = setup_cart(
        [ingredient("морковь")], Catalog(), model_response={"matches": rows}
    )
    result = (await post_cart(app, recipe_id)).json()
    assert result["matches"][0]["product"] is None
    assert result["total"] == 0


@pytest.mark.parametrize("mode", ["unavailable", "malformed", "timeout", "truncated"])
async def test_model_failure_is_partial_error_and_preserves_unsupported_units(
    setup_cart, mode
):
    async def handler(request):
        if mode == "unavailable":
            return httpx.Response(503)
        if mode == "timeout":
            raise httpx.ReadTimeout("secret")
        if mode == "truncated":
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "done_reason": "length",
                    "response": '{"matches":[]}',
                },
            )
        return httpx.Response(200, text="secret malformed data")

    app, recipe_id = setup_cart(
        [ingredient("морковь"), ingredient("чеснок", unit="зубчик")],
        Catalog(),
        model_handler=handler,
    )
    response = await post_cart(app, recipe_id)
    assert response.status_code == 200
    result = response.json()
    assert result["unmatchedCount"] == 2 and result["total"] == 0
    assert result["matches"][0]["error"] not in (
        "Товар не найден",
        "Каталог недоступен",
    )
    assert "secret" not in response.text
    assert result["matches"][1]["error"] == "единица не конвертируется"


async def test_one_invalid_model_position_keeps_valid_result(setup_cart):
    app, recipe_id = setup_cart(
        [ingredient("морковь"), ingredient("молоко")],
        Catalog(),
        model_response={
            "matches": [{"index": 0, "ids": [606]}, {"index": 1, "ids": [606]}]
        },
    )
    result = (await post_cart(app, recipe_id)).json()
    assert result["total"] == 53 and result["unmatchedCount"] == 1
    assert result["matches"][0]["product"]["id"] == 606
    assert result["matches"][1]["error"]


async def test_details_deadline_preserves_other_completed_position(setup_cart):
    cancelled = asyncio.Event()

    class SlowDetails(Catalog):
        async def call(self, tool, arguments):
            if tool == DETAILS and arguments["id"] == 36296:
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return await super().call(tool, arguments)

    app, recipe_id = setup_cart(
        [ingredient("молоко"), ingredient("морковь")],
        SlowDetails(),
        cart_deadline_seconds=0.05,
    )
    result = (await asyncio.wait_for(post_cart(app, recipe_id), 1)).json()
    assert result["total"] == 53 and result["unmatchedCount"] == 1
    assert result["matches"][1]["product"]["id"] == 606
    assert cancelled.is_set()


async def test_changed_full_title_is_not_accepted_even_when_ingredient_matches(
    setup_cart,
):
    case = CATALOG["морковь"]
    catalog = Catalog(
        {
            "морковь": {
                **case,
                "items": [{"id": 606, "name": "Морковь", "unit": "кг"}],
                "details": [{**case["details"][0], "name": "Морковь сушёная"}],
            }
        }
    )
    app, recipe_id = setup_cart([ingredient("морковь")], catalog)
    result = (await post_cart(app, recipe_id)).json()
    assert result["total"] == 0
    assert result["matches"][0]["product"] is None


@pytest.mark.parametrize(
    ("name", "title", "accepted"),
    [
        ("уксус 9%", "Уксус рисовый пищевой", False),
        ("уксус 9%", "Уксус 6%", False),
        ("молоко 2,5%", "Молоко 3.2%", False),
        ("молоко 2,5%", "Молоко 2.50%, 900 мл", True),
        ("молоко 2.5%", "Молоко 2,5–3,2%", False),
        ("молоко 3,0-4,5%", "Молоко 3–4.50%", True),
        ("молоко 3,0-4,5%", "Молоко 3%", False),
    ],
)
async def test_explicit_concentration_survives_wrong_model_selection(
    setup_cart, name, title, accepted
):
    product = {
        "id": 92787,
        "name": title,
        "price": {"current": 100, "currency": "RUB"},
        "unit": "кг",
    }
    case = {
        "items": [product],
        "details": [product],
        "facets": {},
        "expected_id": 92787,
    }
    app, recipe_id = setup_cart(
        [ingredient(name)],
        Catalog({name: case}),
        model_response={"matches": [{"index": 0, "ids": [92787]}]},
    )
    result = (await post_cart(app, recipe_id)).json()
    assert result["allMatched"] is accepted
    assert result["total"] == (100 if accepted else 0)


async def test_vinegar_false_positive_keeps_other_matched_position(setup_cart):
    # Live qwen2.5:7b twice selected rice vinegar without evidence of required 9%.
    reply = {"matches": [{"index": 0, "ids": [606]}, {"index": 1, "ids": [92787]}]}
    app, recipe_id = setup_cart(
        [ingredient("морковь"), ingredient("уксус 9%")], Catalog(), model_response=reply
    )
    result = (await post_cart(app, recipe_id)).json()
    assert result["total"] == 53
    assert result["matches"][1]["product"] is None
    assert result["matches"][1]["error"]


async def test_raw_live_model_reply_cannot_confirm_missing_vinegar_concentration(
    setup_cart,
):
    wire = json.loads((FIXTURES / "cart" / "model-vinegar.json").read_text())
    app, recipe_id = setup_cart(
        [ingredient("уксус 9%")],
        Catalog(),
        model_handler=lambda r: httpx.Response(200, json=wire),
    )
    result = (await post_cart(app, recipe_id)).json()
    assert result["total"] == 0
    assert result["matches"][0]["product"] is None
    assert result["unmatchedCount"] == 1


async def test_initialization_timeout_for_all_searches_is_503(setup_cart):
    from receipes.cart.mcp import McpClient

    async def hanging_initialize(request):
        assert json.loads(request.content)["method"] == "initialize"
        await asyncio.sleep(1)
        return httpx.Response(500)

    mcp = McpClient(
        "http://mcp.test",
        timeout=0.02,
        transport=httpx.MockTransport(hanging_initialize),
    )
    app, recipe_id = setup_cart(
        [ingredient("морковь"), ingredient("молоко")],
        mcp,
        cart_deadline_seconds=0.175,
    )
    try:
        response = await asyncio.wait_for(post_cart(app, recipe_id), 1)
    finally:
        await mcp.aclose()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EXTERNAL_SERVICE_ERROR"


async def test_invalid_catalog_unicode_preserves_other_position(setup_cart):
    milk = {**CATALOG["молоко"], "items": [{"id": 36296, "name": "\ud800"}]}
    app, recipe_id = setup_cart(
        [ingredient("молоко"), ingredient("морковь")],
        Catalog({"молоко": milk, "морковь": CATALOG["морковь"]}),
    )
    response = await post_cart(app, recipe_id)
    assert response.status_code == 200
    result = response.json()
    assert result["matches"][0]["product"] is None
    assert result["matches"][0]["error"]
    assert result["matches"][1]["product"]["id"] == 606
    assert result["total"] == 53

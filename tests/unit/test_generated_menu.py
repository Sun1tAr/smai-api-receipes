import copy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from receipes.generated_menu import GeneratedMenu


def recipe(name: str = "Овсяная каша", **extra: object) -> dict:
    return {
        "name": name,
        "ingredients": [
            {"name": "Овсяные хлопья", "quantity": 200, "unit": "г"},
            {"name": "Молоко", "quantity": 500, "unit": "мл"},
        ],
        "calories": 320,
        "nutrition": {"calories": 320, "proteins": 12, "fats": 8, "carbohydrates": 50},
        "cookingTime": 15,
        "instructions": ["Вскипятить молоко.", "Добавить хлопья."],
        **extra,
    }


def payload(count: int = 3) -> dict:
    return {"recipes": [recipe(f"Блюдо {index}") for index in range(count)]}


def invalid(mutate) -> ValidationError:
    data = payload()
    mutate(data)
    with pytest.raises(ValidationError) as error:
        GeneratedMenu.model_validate(data)
    return error.value


def test_valid_menu_uses_camel_case_and_keeps_order() -> None:
    menu = GeneratedMenu.model_validate(payload())
    assert [item.name for item in menu.recipes] == ["Блюдо 0", "Блюдо 1", "Блюдо 2"]
    first = menu.recipes[0]
    assert first.cooking_time == Decimal("15.00")
    assert first.nutrition.proteins == Decimal("12.00")
    assert first.ingredients[0].quantity == Decimal("200.00")


def test_snake_case_names_are_also_accepted_for_internal_use() -> None:
    data = payload()
    data["recipes"][0]["cooking_time"] = data["recipes"][0].pop("cookingTime")
    assert GeneratedMenu.model_validate(data).recipes[0].cooking_time == 15


@pytest.mark.parametrize("count", [3, 21])
def test_recipe_count_bounds_accepted(count: int) -> None:
    assert len(GeneratedMenu.model_validate(payload(count)).recipes) == count


@pytest.mark.parametrize("count", [0, 2, 22])
def test_recipe_count_bounds_rejected(count: int) -> None:
    with pytest.raises(ValidationError):
        GeneratedMenu.model_validate(payload(count))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["recipes"][0].update(ingredients=[]),
        lambda d: d["recipes"][0].update(
            ingredients=[
                {"name": f"i{n}", "quantity": 1, "unit": "г"} for n in range(51)
            ]
        ),
        lambda d: d["recipes"][0].update(instructions=[]),
        lambda d: d["recipes"][0].update(instructions=["шаг"] * 51),
        lambda d: d["recipes"][0].update(name="   "),
        lambda d: d["recipes"][0].update(name=""),
        lambda d: d["recipes"][0]["instructions"].append("  "),
        lambda d: d["recipes"][0]["ingredients"][0].update(name=" "),
        lambda d: d["recipes"][0]["ingredients"][0].update(unit=""),
        lambda d: d["recipes"][0].update(calories=-1),
        lambda d: d["recipes"][0].update(calories=10001),
        lambda d: d["recipes"][0].update(cookingTime=-1),
        lambda d: d["recipes"][0].update(cookingTime=1441),
        lambda d: d["recipes"][0]["nutrition"].update(fats=-0.1),
        lambda d: d["recipes"][0]["nutrition"].update(proteins=10001),
        lambda d: d["recipes"][0]["ingredients"][0].update(quantity=-1),
        lambda d: d["recipes"][0]["ingredients"][0].update(quantity=100001),
        lambda d: d["recipes"][0].update(name="я" * 256),
        lambda d: d["recipes"][0]["ingredients"][0].update(name="я" * 256),
        lambda d: d["recipes"][0]["ingredients"][0].update(unit="г" * 33),
        lambda d: d["recipes"][0].update(instructions=["ш" * 10001]),
        lambda d: d["recipes"][0].pop("nutrition"),
        lambda d: d["recipes"][0].pop("cookingTime"),
        lambda d: d["recipes"][0].pop("instructions"),
    ],
)
def test_out_of_bounds_values_are_rejected(mutate) -> None:
    invalid(mutate)


@pytest.mark.parametrize("value", ["NaN", "Infinity", float("nan"), float("inf")])
def test_non_finite_numbers_are_rejected(value) -> None:
    invalid(lambda d: d["recipes"][0].update(calories=value))
    invalid(lambda d: d["recipes"][0]["nutrition"].update(fats=value))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["recipes"][0].update(name="я" * 255),
        lambda d: d["recipes"][0]["ingredients"][0].update(unit="г" * 32),
        lambda d: d["recipes"][0].update(instructions=["ш" * 10000]),
        lambda d: d["recipes"][0].update(calories=10000, cookingTime=1440),
        lambda d: d["recipes"][0]["ingredients"][0].update(quantity=0),
    ],
)
def test_boundary_values_are_accepted(mutate) -> None:
    data = payload()
    mutate(data)
    GeneratedMenu.model_validate(data)


def test_numbers_are_rounded_to_hundredths() -> None:
    data = payload()
    data["recipes"][0]["calories"] = 320.456
    data["recipes"][0]["ingredients"][0]["quantity"] = 0.005
    menu = GeneratedMenu.model_validate(data)
    assert menu.recipes[0].calories == Decimal("320.46")
    assert menu.recipes[0].ingredients[0].quantity == Decimal("0.01")


def test_photo_from_the_model_is_ignored() -> None:
    data = payload()
    data["recipes"][0]["photo"] = "https://evil.example/x.jpg"
    menu = GeneratedMenu.model_validate(data)
    assert menu.recipes[0].photo is None
    assert "evil" not in menu.model_dump_json(by_alias=True)


def test_unknown_fields_are_ignored() -> None:
    data = payload()
    data["recipes"][0]["comment"] = "ignored"
    data["note"] = "ignored"
    GeneratedMenu.model_validate(data)


def test_duplicate_ingredients_with_same_unit_are_summed() -> None:
    data = payload()
    data["recipes"][0]["ingredients"] = [
        {"name": "Молоко", "quantity": 200, "unit": "мл"},
        {"name": "  молоко ", "quantity": 300.5, "unit": "МЛ"},
        {"name": "Мёд", "quantity": 1, "unit": "ложка"},
        {"name": "мед", "quantity": 2, "unit": "ложка"},
    ]
    ingredients = GeneratedMenu.model_validate(data).recipes[0].ingredients
    assert [(i.name, i.quantity) for i in ingredients] == [
        ("Молоко", Decimal("500.50")),
        ("Мёд", Decimal("3.00")),
    ]


def test_duplicate_ingredients_with_different_unit_keep_first_and_warn() -> None:
    from structlog.testing import capture_logs

    data = payload()
    data["recipes"][0]["ingredients"] = [
        {"name": "Молоко", "quantity": 200, "unit": "мл"},
        {"name": "молоко", "quantity": 1, "unit": "стакан"},
    ]
    with capture_logs() as logs:
        ingredients = GeneratedMenu.model_validate(data).recipes[0].ingredients
    assert [(i.quantity, i.unit) for i in ingredients] == [(Decimal("200.00"), "мл")]
    assert [e["event"] for e in logs] == ["duplicate_ingredient_dropped"]
    assert logs[0]["log_level"] == "warning"


def test_same_ingredient_in_different_recipes_is_not_merged() -> None:
    menu = GeneratedMenu.model_validate(payload())
    assert all(len(item.ingredients) == 2 for item in menu.recipes)


def test_input_is_not_mutated_and_strings_are_stripped() -> None:
    data = payload()
    data["recipes"][0]["name"] = "  Каша  "
    original = copy.deepcopy(data)
    menu = GeneratedMenu.model_validate(data)
    assert menu.recipes[0].name == "Каша"
    assert data == original


def test_dump_by_alias_uses_the_contract_names() -> None:
    dumped = GeneratedMenu.model_validate(payload()).model_dump(by_alias=True)
    assert "cookingTime" in dumped["recipes"][0]
    assert "photo" not in dumped["recipes"][0]


TEXT_FIELDS = {
    "recipe name": lambda d, v: d["recipes"][0].update(name=v),
    "ingredient name": lambda d, v: d["recipes"][0]["ingredients"][0].update(name=v),
    "unit": lambda d, v: d["recipes"][0]["ingredients"][0].update(unit=v),
    "step": lambda d, v: d["recipes"][0].update(instructions=["шаг", v]),
}


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("bad", ["a\x00b", "\x00", "x\ud800y"])
def test_nul_and_surrogates_are_rejected_in_every_text_field(field, bad) -> None:
    invalid(lambda d: TEXT_FIELDS[field](d, bad))


def test_photo_with_nul_is_ignored_not_stored() -> None:
    data = payload()
    data["recipes"][0]["photo"] = "http://x/\x00"
    assert GeneratedMenu.model_validate(data).recipes[0].photo is None


def test_ingredient_keeps_the_display_name_and_derives_the_key() -> None:
    data = payload()
    data["recipes"][0]["ingredients"][0]["name"] = "  Мёд   ЛИПОВЫЙ "
    ingredient = GeneratedMenu.model_validate(data).recipes[0].ingredients[0]
    assert ingredient.name == "Мёд ЛИПОВЫЙ"
    assert ingredient.name_key == "мед липовый"


def test_merged_quantity_above_the_limit_is_not_summed() -> None:
    from structlog.testing import capture_logs

    data = payload()
    data["recipes"][0]["ingredients"] = [
        {"name": "Соль", "quantity": 60000, "unit": "г"},
        {"name": "соль", "quantity": 60000, "unit": "г"},
    ]
    with capture_logs() as logs:
        ingredients = GeneratedMenu.model_validate(data).recipes[0].ingredients
    assert [i.quantity for i in ingredients] == [Decimal("60000.00")]
    assert [e["event"] for e in logs] == ["duplicate_ingredient_sum_too_large"]


def test_ollama_format_schema_accepts_what_the_gate_accepts() -> None:
    import jsonschema

    from receipes.generated_menu import ollama_format_schema

    schema = ollama_format_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(payload(), schema)
    assert "photo" not in str(schema)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["recipes"].pop(),  # меньше трёх рецептов
        lambda d: d["recipes"][0].update(ingredients=[]),
        lambda d: d["recipes"][0].update(instructions=[]),
        lambda d: d["recipes"][0].update(calories=-1),
        lambda d: d["recipes"][0].update(cookingTime=1441),
        lambda d: d["recipes"][0].pop("nutrition"),
        lambda d: d["recipes"][0]["ingredients"][0].update(unit="г" * 33),
    ],
)
def test_ollama_format_schema_rejects_what_the_gate_rejects(mutate) -> None:
    import jsonschema

    from receipes.generated_menu import ollama_format_schema

    data = payload()
    mutate(data)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, ollama_format_schema())
    with pytest.raises(ValidationError):
        GeneratedMenu.model_validate(data)

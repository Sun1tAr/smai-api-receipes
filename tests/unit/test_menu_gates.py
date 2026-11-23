import copy
from decimal import Decimal
from typing import Any

import pytest
from jsonschema import validate
from menu_samples import sample_menu, sample_recipe

from receipes.domain.errors import InvalidNeurocoreResponse
from receipes.domain.menu import parse_menu
from receipes.generation.ollama import RESPONSE_FORMAT


def rejected_reason(data: object) -> str:
    with pytest.raises(InvalidNeurocoreResponse) as error:
        parse_menu(data)
    assert error.value.message == "Invalid neurocore response"
    return error.value.reason


def menu_with(**changes: Any) -> dict[str, Any]:
    menu = sample_menu()
    menu["recipes"][0].update(changes)
    return menu


def test_valid_menu_passes_and_matches_the_ollama_schema() -> None:
    data = sample_menu()
    validate(data, RESPONSE_FORMAT)
    menu = parse_menu(data)
    assert len(menu.recipes) == 3
    assert menu.recipes[0].cooking_time == Decimal("25.00")


def test_format_literal_is_the_one_from_api3() -> None:
    recipes = RESPONSE_FORMAT["properties"]["recipes"]
    assert recipes["minItems"] == 3 and "maxItems" not in recipes
    item = recipes["items"]
    assert item["properties"]["photo"] == {"type": ["string", "null"]}
    assert item["required"] == [
        "name",
        "ingredients",
        "calories",
        "nutrition",
        "cookingTime",
        "instructions",
    ]
    assert RESPONSE_FORMAT["required"] == ["recipes"]


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "text",
        {},
        {"recipes": "x"},
        {"recipes": [{"name": "A"}] * 3},
    ],
)
def test_wrong_structure_is_schema_reason(data: object) -> None:
    assert rejected_reason(data) == "schema"


@pytest.mark.parametrize("value", ["35", True, None, float("nan"), float("inf"), [1]])
def test_numbers_must_be_finite_json_numbers_not_strings(value: object) -> None:
    assert rejected_reason(menu_with(calories=value)) == "schema"


def test_string_fields_must_be_strings() -> None:
    data = sample_menu()
    data["recipes"][0]["instructions"] = [1, 2]
    assert rejected_reason(data) == "schema"


@pytest.mark.parametrize("count", [0, 1, 2])
def test_fewer_than_three_recipes_is_count_reason(count: int) -> None:
    data = sample_menu()
    data["recipes"] = data["recipes"][:count]
    assert rejected_reason(data) == "count"


@pytest.mark.parametrize("count", [3, 14, 40])
def test_three_or_more_recipes_have_no_upper_limit(count: int) -> None:
    data = {"recipes": [sample_recipe(f"Блюдо {i}") for i in range(count)]}
    assert len(parse_menu(data).recipes) == count


@pytest.mark.parametrize("field", ["ingredients", "instructions"])
def test_recipe_needs_ingredient_and_step(field: str) -> None:
    assert rejected_reason(menu_with(**{field: []})) == "count"


def test_numbers_are_rounded_to_two_places_before_checks() -> None:
    data = menu_with()
    data["recipes"][0]["ingredients"][0]["quantity"] = 0.126
    data["recipes"][0]["calories"] = 350.499
    menu = parse_menu(data)
    assert menu.recipes[0].ingredients[0].quantity == Decimal("0.13")
    assert menu.recipes[0].calories == Decimal("350.50")


def test_quantity_that_rounds_to_zero_is_rejected() -> None:
    data = menu_with()
    data["recipes"][0]["ingredients"][0]["quantity"] = 0.004
    assert rejected_reason(data) == "bounds"


@pytest.mark.parametrize("quantity", [0, -1])
def test_non_positive_quantity_is_bounds(quantity: float) -> None:
    data = menu_with()
    data["recipes"][0]["ingredients"][0]["quantity"] = quantity
    assert rejected_reason(data) == "bounds"


def test_zero_calories_and_time_are_allowed_but_negative_are_not() -> None:
    parse_menu(menu_with(calories=0, cookingTime=0))
    assert rejected_reason(menu_with(calories=-1)) == "bounds"
    assert rejected_reason(menu_with(cookingTime=-5)) == "bounds"
    data = menu_with()
    data["recipes"][0]["nutrition"]["fats"] = -0.5
    assert rejected_reason(data) == "bounds"


@pytest.mark.parametrize("value", [100_000_000, 99999999.995])
def test_numbers_above_column_limit_are_bounds(value: float) -> None:
    assert rejected_reason(menu_with(calories=value)) == "bounds"


def test_length_limits_are_bounds() -> None:
    assert rejected_reason(menu_with(name="а" * 201)) == "bounds"
    parse_menu(menu_with(name="а" * 200))
    long_step = menu_with(instructions=["б" * 2001])
    assert rejected_reason(long_step) == "bounds"
    assert rejected_reason(menu_with(instructions=["шаг"] * 31)) == "bounds"
    ingredients = [{"name": f"П{i}", "quantity": 1, "unit": "г"} for i in range(51)]
    assert rejected_reason(menu_with(ingredients=ingredients)) == "bounds"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1. варить", "варить"),
        ("2) нарезать", "нарезать"),
        ("  3 . обжарить", "обжарить"),
        ("1.5 часа томить", "1.5 часа томить"),
        ("Нагреть до 180 градусов", "Нагреть до 180 градусов"),
    ],
)
def test_step_numbering_is_stripped_only_at_the_start(raw: str, expected: str) -> None:
    menu = parse_menu(menu_with(instructions=[raw]))
    assert menu.recipes[0].instructions == [expected]


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "1.", "2)", "\x00", "a‮b", "строка вторая", "a\nb", "a\tb"],
)
def test_empty_or_forbidden_characters_in_steps_are_text_reason(bad: str) -> None:
    assert rejected_reason(menu_with(instructions=[bad])) == "text"


@pytest.mark.parametrize("bad", ["", "  ", "Суп\nдня", "Суп​", "\x07"])
def test_recipe_names_follow_the_same_text_rules(bad: str) -> None:
    assert rejected_reason(menu_with(name=bad)) == "text"


def test_empty_unit_and_ingredient_name_are_text_reason() -> None:
    data = menu_with()
    data["recipes"][0]["ingredients"][0]["unit"] = ""
    assert rejected_reason(data) == "text"
    data = menu_with()
    data["recipes"][0]["ingredients"][0]["name"] = "   "
    assert rejected_reason(data) == "text"


def test_names_are_normalized_for_storage() -> None:
    menu = parse_menu(menu_with(name="  Тушёная\u00a0 говядина "))
    assert menu.recipes[0].name == "Тушёная говядина"


@pytest.mark.parametrize(
    "name", ["200 г муки", "200г муки", "1,5 кг картофеля", "2 мл масла", "3 шт яиц"]
)
def test_ingredient_name_starting_with_quantity_and_unit_is_rejected(name: str) -> None:
    data = menu_with()
    data["recipes"][0]["ingredients"][0]["name"] = name
    assert rejected_reason(data) == "ingredient_name"


@pytest.mark.parametrize("name", ["Масло 82%", "Молоко 2,5%", "Мука", "Гречка"])
def test_digits_in_the_middle_or_not_a_unit_are_allowed(name: str) -> None:
    data = menu_with()
    data["recipes"][0]["ingredients"][0]["name"] = name
    assert parse_menu(data).recipes[0].ingredients[0].name == name


@pytest.mark.parametrize("second", ["Соль", "соль", " Соль  ", "«Соль»", "Соль."])
def test_duplicate_ingredient_in_a_recipe_is_rejected(second: str) -> None:
    data = menu_with()
    data["recipes"][0]["ingredients"] = [
        {"name": "Соль", "quantity": 1, "unit": "г"},
        {"name": second, "quantity": 2, "unit": "г"},
    ]
    assert rejected_reason(data) == "duplicate_ingredient"


def test_same_ingredient_in_different_recipes_is_fine() -> None:
    data = sample_menu()
    for recipe in data["recipes"]:
        recipe["ingredients"] = [{"name": "Соль", "quantity": 1, "unit": "г"}]
    parse_menu(data)


@pytest.mark.parametrize(
    "second",
    [
        "сырники",
        "Сырники.",
        "Сырники. ",
        "«Сырники»",
        "„Сырники“",
        "СЫРНИКИ",
        "Сыр ники".replace(" ", ""),
    ],
)
def test_duplicate_dish_names_in_one_menu_are_rejected(second: str) -> None:
    data = sample_menu()
    data["recipes"][0]["name"] = "Сырники"
    data["recipes"][1]["name"] = second
    assert rejected_reason(data) == "duplicate_name"


def test_yo_double_space_and_nbsp_are_folded_in_dish_names() -> None:
    data = sample_menu()
    data["recipes"][0]["name"] = "Мёд  липовый"
    data["recipes"][1]["name"] = "мед липовый"
    assert rejected_reason(data) == "duplicate_name"


def test_similar_but_different_dishes_are_allowed() -> None:
    data = sample_menu()
    data["recipes"][0]["name"] = "Омлет с сыром"
    data["recipes"][1]["name"] = "Омлет с сыром и зеленью"
    parse_menu(data)


def test_photo_from_the_model_is_dropped() -> None:
    data = menu_with(photo="https://example.com/x.jpg")
    menu = parse_menu(copy.deepcopy(data))
    assert not hasattr(menu.recipes[0], "photo")


@pytest.mark.parametrize("value", [1e30, 10**40, -(10**40), 1e308])
@pytest.mark.parametrize("field", ["calories", "cookingTime"])
def test_numbers_beyond_decimal_precision_are_bounds_not_a_crash(
    field: str, value: float
) -> None:
    assert rejected_reason(menu_with(**{field: value})) == "bounds"


def test_huge_quantity_is_bounds() -> None:
    data = menu_with()
    data["recipes"][0]["ingredients"][0]["quantity"] = 10**40
    assert rejected_reason(data) == "bounds"
    data["recipes"][0]["ingredients"][0]["quantity"] = 1e30
    assert rejected_reason(data) == "bounds"


@pytest.mark.parametrize("field", ["quantity", "unit"])
def test_missing_ingredient_amount_is_rejected(field: str) -> None:
    data = sample_menu()
    del data["recipes"][1]["ingredients"][0][field]
    assert rejected_reason(data) == "schema"

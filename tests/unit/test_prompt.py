from decimal import Decimal

import pytest

from receipes.domain import GenerationParameters, InvalidPromptTemplate
from receipes.prompt import build_prompt, format_budget, prompt_builder

TEMPLATE = (
    "Меню на {people_count} чел., бюджет {weekly_budget} руб.\n"
    'Формат: {"recipes": [{"name": "строка", "tags": {}}]}\n'
    "Ещё раз бюджет: {weekly_budget}."
)


def parameters(people: int = 2, budget: str = "1500.5") -> GenerationParameters:
    return GenerationParameters(people, Decimal(budget))


def test_parameters_are_substituted_everywhere() -> None:
    prompt = build_prompt(TEMPLATE, parameters(3, "1500.5"))
    assert prompt.startswith("Меню на 3 чел., бюджет 1500.50 руб.")
    assert prompt.endswith("Ещё раз бюджет: 1500.50.")
    assert "{people_count}" not in prompt and "{weekly_budget}" not in prompt


def test_json_braces_in_the_template_survive_substitution() -> None:
    prompt = build_prompt(TEMPLATE, parameters())
    assert '{"recipes": [{"name": "строка", "tags": {}}]}' in prompt


def test_template_with_percent_and_dollar_signs() -> None:
    template = "{people_count} / {weekly_budget} 100% $5 {1} { }"
    assert build_prompt(template, parameters()) == "2 / 1500.50 100% $5 {1} { }"


def test_str_format_style_escaping_is_not_supported() -> None:
    with pytest.raises(InvalidPromptTemplate):
        build_prompt("{people_count} {weekly_budget} {{x}}", parameters())


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        ("1000", "1000.00"),
        ("1E+3", "1000.00"),
        ("0.5", "0.50"),
        ("0.01", "0.01"),
        ("999999.99", "999999.99"),
        ("1000000", "1000000.00"),
        ("12.340", "12.34"),
    ],
)
def test_budget_is_formatted_with_two_places_and_no_exponent(budget, expected) -> None:
    assert format_budget(Decimal(budget)) == expected
    assert "e" not in build_prompt(
        "{people_count}{weekly_budget}", parameters(1, budget)
    )


@pytest.mark.parametrize(
    "template", ["бюджет {weekly_budget}", "людей {people_count}", "без плейсхолдеров"]
)
def test_missing_placeholder_is_a_domain_error(template: str) -> None:
    with pytest.raises(InvalidPromptTemplate) as error:
        build_prompt(template, parameters())
    assert "missing" in error.value.reason


@pytest.mark.parametrize("extra", ["{budget}", "{people}", "{x_y}"])
def test_unknown_placeholder_is_a_domain_error(extra: str) -> None:
    with pytest.raises(InvalidPromptTemplate) as error:
        build_prompt(f"{{people_count}} {{weekly_budget}} {extra}", parameters())
    assert extra in error.value.reason


def test_prompt_builder_binds_the_template() -> None:
    build = prompt_builder(TEMPLATE)
    assert build(parameters(5, "10")).startswith("Меню на 5 чел., бюджет 10.00")
    with pytest.raises(InvalidPromptTemplate):
        prompt_builder("{people_count}")(parameters())

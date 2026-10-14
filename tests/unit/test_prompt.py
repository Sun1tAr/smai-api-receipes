import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from receipes.domain.errors import GenerationError, PromptTemplateError
from receipes.generation.prompt import (
    PLACEHOLDERS,
    format_number,
    render_prompt,
    validate_template,
)

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0002_seed_prompt_template.py"
)


def seeded_template() -> str:
    spec = importlib.util.spec_from_file_location("seed_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TEMPLATE


def test_render_substitutes_both_values_everywhere() -> None:
    result = render_prompt(
        "{people_count} чел., бюджет {weekly_budget}, на {people_count}",
        4,
        Decimal("5000"),
    )
    assert result == "4 чел., бюджет 5000, на 4"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("5000"), "5000"),
        (Decimal("5000.00"), "5000"),
        (Decimal("5000.50"), "5000.5"),
        (Decimal("0.01"), "0.01"),
        (Decimal("1000000"), "1000000"),
        (Decimal("1E+3"), "1000"),
        (7, "7"),
    ],
)
def test_numbers_have_no_trailing_zeros_or_exponent(
    value: Decimal | int, text: str
) -> None:
    assert format_number(value) == text


def test_render_is_a_single_pass_and_values_cannot_inject_placeholders() -> None:
    # Подставленное значение не должно повторно обрабатываться как шаблон.
    template = "{people_count} {weekly_budget}"
    assert render_prompt(template, 1, Decimal("2")) == "1 2"


@pytest.mark.parametrize(
    "template",
    [
        "только {people_count}",
        "только {weekly_budget}",
        "{people_count} {weekly_budget} {extra}",
        "{people_count} {weekly_budget} {}",
        "без плейсхолдеров",
    ],
)
def test_template_with_missing_or_foreign_placeholder_is_rejected(
    template: str,
) -> None:
    with pytest.raises(PromptTemplateError) as error:
        render_prompt(template, 1, Decimal("1"))
    assert isinstance(error.value, GenerationError)
    assert error.value.message == "Menu generation failed"


def test_literal_braces_free_text_is_left_untouched() -> None:
    template = "Шаг 1. {people_count} {weekly_budget} «ответ»"
    validate_template(template)
    assert render_prompt(template, 2, Decimal("10")) == "Шаг 1. 2 10 «ответ»"


def test_seeded_template_has_exactly_the_two_placeholders_and_no_values() -> None:
    template = seeded_template()
    validate_template(template)
    assert template.count("{people_count}") == 3
    assert template.count("{weekly_budget}") == 1
    rendered = render_prompt(template, 4, Decimal("5000.5"))
    assert "{" not in rendered and "}" not in rendered
    assert "для 4 человек" in rendered
    assert "5000.5 рублей" in rendered
    assert PLACEHOLDERS == {"people_count", "weekly_budget"}


def test_seeded_template_carries_the_agreed_rules() -> None:
    template = seeded_template()
    assert "не менее 3 разных блюд" in template
    assert "поле photo всегда null" in template
    assert "на одну порцию" in template

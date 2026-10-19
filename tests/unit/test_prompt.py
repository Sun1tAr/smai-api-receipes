import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from receipes.dictionaries import Dictionaries
from receipes.domain.errors import (
    GenerationError,
    PromptTemplateError,
    UnknownDictionaryId,
)
from receipes.domain.params import GenerationParams
from receipes.generation.prompt import (
    PLACEHOLDERS,
    build_constraints,
    format_number,
    needs_dictionaries,
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
        "{{people_count}} {weekly_budget}",
        "{people_count} {weekly_budget}}}",
        "{people_count} {weekly_budget} {",
        "{people_count} {weekly_budget} }",
        "{people_count} {weekly_budget} {{literal}}",
        "{people_count} {weekly_budget} {{",
        "{{ {people_count} {weekly_budget}",
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


# --- Ограничения v2 (SMAI-89) ---

DICTIONARIES = Dictionaries(
    habits={1: "Вегетарианство", 2: "Без сахара", 3: "Low-carb"},
    diets={4: "Кето", 5: "Безглютеновая"},
    allergies={1: "Глютен", 2: "Лактоза", 3: "Орехи, арахис"},
    kitchen_tools={1: "Духовка", 6: "Блендер"},
)


def params(**values: object) -> GenerationParams:
    return GenerationParams(people_count=2, weekly_budget=Decimal("100"), **values)  # type: ignore[arg-type]


def test_empty_selection_gives_empty_block_and_unchanged_prompt() -> None:
    assert build_constraints(params(), DICTIONARIES) == ""
    template = seeded_template()
    plain = render_prompt(template, 2, Decimal("100"))
    assert render_prompt(template, 2, Decimal("100"), "") == plain
    assert not needs_dictionaries(params())


def test_full_selection_block_has_fixed_order_and_names() -> None:
    block = build_constraints(
        params(habits=(3, 1), diet=4, allergies=(2, 1), kitchen_tools=(6, 1)),
        DICTIONARIES,
    )
    assert block == (
        "Ограничения пользователя (обязательны к выполнению):\n"
        "- привычки питания: «Вегетарианство», «Low-carb»;\n"
        "- диета: «Кето»;\n"
        "- аллергены (не использовать ни в одном блюде и ингредиенте): "
        "«Глютен», «Лактоза»;\n"
        "- доступная кухонная техника (не требуй другой): «Духовка», «Блендер»."
    )


@pytest.mark.parametrize(
    ("selection", "absent"),
    [
        ({"habits": (1,)}, ("диета", "аллергены", "техника")),
        ({"diet": 5}, ("привычки", "аллергены", "техника")),
        ({"allergies": (1,)}, ("привычки", "диета", "техника")),
        ({"kitchen_tools": (1,)}, ("привычки", "диета", "аллергены")),
    ],
)
def test_only_selected_groups_make_lines(
    selection: dict[str, object], absent: tuple[str, ...]
) -> None:
    block = build_constraints(params(**selection), DICTIONARIES)
    assert block.count("\n- ") == 1
    assert all(word not in block for word in absent)


def test_block_is_reproducible_and_independent_of_input_order() -> None:
    first = build_constraints(params(habits=(3, 1, 3), allergies=(2, 1)), DICTIONARIES)
    second = build_constraints(params(habits=(1, 3), allergies=(1, 2)), DICTIONARIES)
    assert first == second


def test_names_are_collapsed_and_quoted() -> None:
    dictionaries = Dictionaries(
        habits={1: "  Без \n сахара  "},
        diets={1: "x"},
        allergies={1: "{people_count}"},
        kitchen_tools={1: "Гриль, духовка; плита"},
    )
    block = build_constraints(
        params(habits=(1,), allergies=(1,), kitchen_tools=(1,)), dictionaries
    )
    assert "«Без сахара»" in block
    assert "«{people_count}»" in block
    assert "«Гриль, духовка; плита»" in block


@pytest.mark.parametrize(
    "selection",
    [
        {"habits": (99,)},
        {"diet": 99},
        {"allergies": (99,)},
        {"kitchen_tools": (99,)},
        {"habits": (1,), "diet": 5, "allergies": (1,), "kitchen_tools": (7,)},
    ],
)
def test_unknown_id_is_an_error_without_the_value(selection: dict[str, object]) -> None:
    with pytest.raises(UnknownDictionaryId) as error:
        build_constraints(params(**selection), DICTIONARIES)
    # Исключение без текста: значения ID в сообщение не попадают.
    assert error.value.args == ()


def test_values_from_dictionaries_do_not_pass_through_the_template() -> None:
    dictionaries = Dictionaries(
        habits={}, diets={}, allergies={1: "{weekly_budget}"}, kitchen_tools={}
    )
    block = build_constraints(params(allergies=(1,)), dictionaries)
    result = render_prompt(
        "{people_count} {weekly_budget}\n\nКонец", 1, Decimal("2"), block
    )
    assert "«{weekly_budget}»" in result
    assert result.startswith("1 2\n\n")


def test_block_goes_before_the_last_paragraph() -> None:
    result = render_prompt(
        "{people_count} {weekly_budget}\n\nсередина\n\nИтог  \n\n",
        1,
        Decimal("2"),
        "БЛОК",
    )
    assert result == "1 2\n\nсередина\n\nБЛОК\n\nИтог"


def test_block_goes_to_the_end_when_there_are_no_paragraphs() -> None:
    result = render_prompt("{people_count} {weekly_budget}", 1, Decimal("2"), "БЛОК")
    assert result == "1 2\n\nБЛОК"


def test_seeded_template_gets_the_block_byte_for_byte_before_the_final_line() -> None:
    template = seeded_template()
    block = build_constraints(params(habits=(1,), diet=4), DICTIONARIES)
    plain = render_prompt(template, 2, Decimal("100"))
    head, _, last = plain.rpartition("\n\n")
    assert last.startswith("Верни только JSON")
    expected = f"{head}\n\n{block}\n\n{last}"
    assert render_prompt(template, 2, Decimal("100"), block) == expected
    assert expected.endswith(last)


def test_broken_template_is_rejected_even_with_constraints() -> None:
    with pytest.raises(PromptTemplateError):
        render_prompt("нет плейсхолдеров", 1, Decimal("1"), "БЛОК")


# Справочники MS-2 из сида (SMAI-86, tests/spec_data.py): копия на момент SMAI-89.
SEED = Dictionaries(
    habits=dict(
        enumerate(
            [
                "Быстрые перекусы",
                "Низкокалорийные блюда",
                "Полезная еда",
                "Еда с собой",
                "Высокобелковая",
                "Для семейных приёмов пищи",
            ],
            start=1,
        )
    ),
    diets=dict(enumerate(["Веган", "Вегетарианец", "Кето", "Средиземноморская"], 1)),
    allergies=dict(
        enumerate(
            [
                "Глютен",
                "Лактоза",
                "Яйца",
                "Арахис",
                "Древесные орехи",
                "Соя",
                "Рыба",
                "Ракообразные",
                "Моллюски",
                "Кунжут",
                "Горчица",
                "Сельдерей",
                "Люпин",
                "Сульфиты",
                "Пшеница",
                "Кукуруза",
                "Мёд",
                "Цитрусовые",
                "Какао / шоколад",
                "Клубника",
            ],
            start=1,
        )
    ),
    kitchen_tools=dict(
        enumerate(
            [
                "Плита",
                "Духовка",
                "Микроволновая печь",
                "Аэрогриль",
                "Мультиварка",
                "Блендер",
                "Тостер",
                "Пароварка",
                "Электрический гриль",
                "Фритюрница",
                "Миксер",
                "Су-вид",
                "Вафельница",
                "Хлебопечка",
                "Кофемашина",
            ],
            start=1,
        )
    ),
)


# Худший случай по сиду: 3 самые длинные привычки, самая длинная диета, все 20
# аллергенов и вся техника (15). Потолок символов защищает резерв контекста
# (CONTEXT_RESERVE_TOKENS) от роста формулировок и справочников.
def test_worst_case_block_fits_the_context_reserve() -> None:
    from receipes.config import CONTEXT_RESERVE_TOKENS

    longest_habits = sorted(SEED.habits, key=lambda i: len(SEED.habits[i]))[-3:]
    block = build_constraints(
        params(
            habits=tuple(longest_habits),
            diet=4,
            allergies=tuple(SEED.allergies),
            kitchen_tools=tuple(SEED.kitchen_tools),
        ),
        SEED,
    )
    assert len(block) <= 800
    # Замер qwen2.5:7b: 717 символов блока = 334 токена (~2,15 символа на токен);
    # берём 2 с запасом. Шаблон без блока: 822 токена.
    assert len(block) / 2 + 822 <= CONTEXT_RESERVE_TOKENS

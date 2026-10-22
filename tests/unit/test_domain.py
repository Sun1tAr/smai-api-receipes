import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, PrimaryKeyConstraint

from receipes.database import Base
from receipes.db import models  # noqa: F401
from receipes.domain.names import name_key, normalize_name
from receipes.domain.status import ALLOWED_TRANSITIONS, TaskStatus, can_transition

ERD_COLUMNS = {
    "prompt_template": {"id", "template", "created_at"},
    "generation_task": {
        "task_id",
        "status",
        "prompt",
        "parameters_id",
        "menu_id",
        "created_at",
        "status_changed_at",
        "error",
    },
    "generation_parameters": {
        "id",
        "people_count",
        "weekly_budget",
        "diet_id",
        "user_id",
    },
    "generation_parameter_habits": {"id", "parameters_id", "habit_id"},
    "generation_parameter_allergies": {"id", "parameters_id", "allergen_id"},
    "generation_parameter_kitchen_tools": {"id", "parameters_id", "kitchen_tool_id"},
    "menu": {"id"},
    "menu_recipes": {"id", "menu_id", "recipe_id"},
    "recipe": {"id", "name", "calories", "cooking_time", "photo", "nutrition_id"},
    "ingredient": {"id", "name"},
    "recipe_ingredients": {"id", "recipe_id", "ingredient_id", "quantity", "unit"},
    "instruction": {"id", "recipe_id", "text", "position"},
    "nutrition": {"id", "calories", "proteins", "fats", "carbohydrates"},
}
ERD_FOREIGN_KEYS = {
    ("generation_task", "parameters_id", "generation_parameters"),
    ("generation_task", "menu_id", "menu"),
    ("generation_parameter_habits", "parameters_id", "generation_parameters"),
    ("generation_parameter_allergies", "parameters_id", "generation_parameters"),
    ("generation_parameter_kitchen_tools", "parameters_id", "generation_parameters"),
    ("menu_recipes", "menu_id", "menu"),
    ("menu_recipes", "recipe_id", "recipe"),
    ("recipe", "nutrition_id", "nutrition"),
    ("recipe_ingredients", "recipe_id", "recipe"),
    ("recipe_ingredients", "ingredient_id", "ingredient"),
    ("instruction", "recipe_id", "recipe"),
}


def test_tables_and_columns_match_the_erd() -> None:
    tables = Base.metadata.tables
    assert {name: set(table.columns.keys()) for name, table in tables.items()} == (
        ERD_COLUMNS
    )


def test_foreign_keys_match_the_erd_and_have_no_cascade() -> None:
    found = set()
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                found.add(
                    (
                        table.name,
                        constraint.column_keys[0],
                        constraint.referred_table.name,
                    )
                )
                assert constraint.ondelete is None
    assert found == ERD_FOREIGN_KEYS


def test_only_primary_keys_and_foreign_keys_beyond_not_null() -> None:
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            assert isinstance(constraint, PrimaryKeyConstraint | ForeignKeyConstraint)
            assert not isinstance(constraint, CheckConstraint)
        assert all(not index.unique for index in table.indexes)


def test_ids_are_uuid_and_dates_have_time_zone() -> None:
    tables = Base.metadata.tables
    for table in tables.values():
        for column in table.primary_key.columns:
            assert column.type.__class__.__name__ == "Uuid"
    for table, column in (
        ("generation_task", "created_at"),
        ("generation_task", "status_changed_at"),
        ("prompt_template", "created_at"),
    ):
        assert tables[table].c[column].type.timezone is True


def test_task_transitions_follow_le6() -> None:
    assert can_transition(TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
    assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.DONE)
    assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.FAILED)
    # В диаграмме LE-6 нет прямого pending -> failed, конечные состояния закрыты.
    assert not can_transition(TaskStatus.PENDING, TaskStatus.FAILED)
    assert not can_transition(TaskStatus.PENDING, TaskStatus.DONE)
    assert ALLOWED_TRANSITIONS[TaskStatus.DONE] == frozenset()
    assert ALLOWED_TRANSITIONS[TaskStatus.FAILED] == frozenset()
    assert {item.value for item in TaskStatus} == {
        "pending",
        "in_progress",
        "done",
        "failed",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Куриное   филе ", "Куриное филе"),
        ("Куриное филе", "Куриное филе"),
        ("ｆｕｌｌ", "full"),
        ("Яйцо", "Яйцо"),
    ],
)
def test_normalize_name_keeps_case_and_collapses_spaces(
    raw: str, expected: str
) -> None:
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "variant",
    [
        "Сырники",
        "сырники",
        "СЫРНИКИ.",
        "Сырники. ",
        "«Сырники»",
        "„Сырники“",
    ],
)
def test_name_key_ignores_case_quotes_and_trailing_punctuation(variant: str) -> None:
    assert name_key(variant) == name_key("Сырники")


def test_name_key_folds_yo_and_inner_spaces_but_keeps_different_dishes() -> None:
    assert name_key("Мёд  липовый") == name_key("мед липовый")
    assert name_key("Омлет с сыром") != name_key("Омлет с сыром и зеленью")

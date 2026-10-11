from decimal import Decimal
from uuid import uuid4

import pytest

from receipes.domain import (
    GenerationParameters,
    GenerationTaskNotFound,
    InvalidGenerationParameters,
    InvalidTaskTransition,
    MenuNotFound,
    TaskStatus,
    display_ingredient_name,
    normalize_ingredient_name,
)

ALLOWED = {
    (TaskStatus.PENDING, TaskStatus.IN_PROGRESS),
    (TaskStatus.PENDING, TaskStatus.FAILED),
    (TaskStatus.IN_PROGRESS, TaskStatus.DONE),
    (TaskStatus.IN_PROGRESS, TaskStatus.FAILED),
}


@pytest.mark.parametrize("current", list(TaskStatus))
@pytest.mark.parametrize("target", list(TaskStatus))
def test_transition_table_allows_only_the_forward_moves(current, target) -> None:
    assert current.can_transition_to(target) is ((current, target) in ALLOWED)


def test_status_values_match_the_api_contract() -> None:
    assert [status.value for status in TaskStatus] == [
        "pending",
        "in_progress",
        "done",
        "failed",
    ]


def test_domain_errors_carry_context_without_user_data() -> None:
    error = InvalidTaskTransition(TaskStatus.DONE, TaskStatus.PENDING)
    assert (error.current, error.target) == (TaskStatus.DONE, TaskStatus.PENDING)
    task_id = uuid4()
    assert GenerationTaskNotFound(task_id).task_id == task_id
    menu_id = uuid4()
    assert MenuNotFound(menu_id).menu_id == menu_id
    assert InvalidGenerationParameters("people_count", "bad").field == "people_count"


@pytest.mark.parametrize("people", [1, 2, 20])
def test_valid_people_count(people: int) -> None:
    assert GenerationParameters(people, Decimal("100")).people_count == people


@pytest.mark.parametrize("people", [0, -1, 21, 2.0, "3", None, True])
def test_invalid_people_count(people) -> None:
    with pytest.raises(InvalidGenerationParameters) as error:
        GenerationParameters(people, Decimal("100"))
    assert error.value.field == "people_count"


@pytest.mark.parametrize(
    "budget",
    [Decimal("0.01"), Decimal("1000"), Decimal("1000000"), 500, "12.50"],
)
def test_valid_weekly_budget(budget) -> None:
    assert GenerationParameters(1, budget).weekly_budget == Decimal(budget)


@pytest.mark.parametrize(
    "budget",
    [
        Decimal("0"),
        Decimal("-5"),
        Decimal("1000000.01"),
        Decimal("10.001"),
        Decimal("NaN"),
        Decimal("Infinity"),
        10.5,
        "abc",
        None,
        True,
    ],
)
def test_invalid_weekly_budget(budget) -> None:
    with pytest.raises(InvalidGenerationParameters) as error:
        GenerationParameters(1, budget)
    assert error.value.field == "weekly_budget"


def test_trailing_zero_decimals_are_not_extra_places() -> None:
    assert GenerationParameters(1, Decimal("10.500")).weekly_budget == Decimal("10.5")


def test_parameters_are_immutable() -> None:
    parameters = GenerationParameters(2, Decimal("100"))
    with pytest.raises(AttributeError):
        parameters.people_count = 3  # type: ignore[misc]


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("Молоко", "молоко"),
        ("  Молоко  ", "молоко"),
        ("Овсяные   хлопья", "овсяные хлопья"),
        ("овсяные\tхлопья\n", "овсяные хлопья"),
        ("Ёлочный", "елочный"),
        ("МЁД", "мед"),
        ("Straße", "strasse"),
    ],
)
def test_ingredient_name_normalization(raw: str, normalized: str) -> None:
    assert normalize_ingredient_name(raw) == normalized


def test_display_name_keeps_case_and_yo() -> None:
    assert display_ingredient_name("  Мёд   Липовый\t") == "Мёд Липовый"

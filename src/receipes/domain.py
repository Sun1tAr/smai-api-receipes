import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from uuid import UUID

PEOPLE_COUNT_MIN = 1
PEOPLE_COUNT_MAX = 20
WEEKLY_BUDGET_MAX = Decimal("1000000")
BUDGET_DECIMAL_PLACES = 2

# Размеры колонок: «ворота» ответа модели не пропускают строки длиннее.
NAME_MAX_LENGTH = 255
UNIT_MAX_LENGTH = 32
INSTRUCTION_MAX_LENGTH = 2000


class DomainError(Exception):
    """Базовая ошибка домена; текст безопасен для логов, без пользовательских данных."""


class InvalidTaskTransition(DomainError):
    def __init__(self, current: "TaskStatus", target: "TaskStatus") -> None:
        super().__init__(f"Task cannot move from {current} to {target}")
        self.current = current
        self.target = target


class InvalidGenerationParameters(DomainError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"Invalid {field}: {reason}")
        self.field = field
        self.reason = reason


class GenerationTaskNotFound(DomainError):
    def __init__(self, task_id: UUID) -> None:
        super().__init__(f"Generation task {task_id} not found")
        self.task_id = task_id


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"

    def can_transition_to(self, target: "TaskStatus") -> bool:
        return target in ALLOWED_TRANSITIONS[self]


# Единственный источник допустимых переходов: из done и failed выхода нет.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.FAILED}),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.DONE, TaskStatus.FAILED}),
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset(),
}


# Параметры генерации меню: объект-значение, невалидное состояние не создаётся.
@dataclass(frozen=True)
class GenerationParameters:
    people_count: int
    weekly_budget: Decimal

    def __post_init__(self) -> None:
        people = self.people_count
        # bool — подкласс int, но «True человек» не число людей.
        if not isinstance(people, int) or isinstance(people, bool):
            raise InvalidGenerationParameters("people_count", "must be an integer")
        if not PEOPLE_COUNT_MIN <= people <= PEOPLE_COUNT_MAX:
            raise InvalidGenerationParameters(
                "people_count",
                f"must be between {PEOPLE_COUNT_MIN} and {PEOPLE_COUNT_MAX}",
            )
        object.__setattr__(self, "weekly_budget", self._budget(self.weekly_budget))

    # float отвергается: двоичная дробь не передаёт сумму точно.
    @staticmethod
    def _budget(value: object) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
            raise InvalidGenerationParameters(
                "weekly_budget", "must be a decimal number"
            )
        try:
            budget = Decimal(value)
        except InvalidOperation:
            raise InvalidGenerationParameters(
                "weekly_budget", "must be a decimal number"
            ) from None
        if not budget.is_finite():
            raise InvalidGenerationParameters("weekly_budget", "must be finite")
        if not 0 < budget <= WEEKLY_BUDGET_MAX:
            raise InvalidGenerationParameters(
                "weekly_budget",
                f"must be greater than 0 and at most {WEEKLY_BUDGET_MAX}",
            )
        if budget != budget.quantize(Decimal(1).scaleb(-BUDGET_DECIMAL_PLACES)):
            raise InvalidGenerationParameters(
                "weekly_budget",
                f"must have at most {BUDGET_DECIMAL_PLACES} decimal places",
            )
        return budget


_SPACES = re.compile(r"\s+")


# Единственное место нормализации названий ингредиентов: по ней же считается
# уникальность в БД (lower(btrim(name)) от уже нормализованного значения).
def normalize_ingredient_name(name: str) -> str:
    collapsed = _SPACES.sub(" ", unicodedata.normalize("NFC", name)).strip()
    return collapsed.casefold().replace("ё", "е")

from enum import StrEnum


class TaskKind(StrEnum):
    MENU = "menu"
    REPLACE = "replace"


# Статусы задачи генерации (LE-6).
class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


# Допустимые переходы строго по диаграмме LE-6; done и failed конечные.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.IN_PROGRESS}),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.DONE, TaskStatus.FAILED}),
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset(),
}


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]

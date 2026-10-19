from dataclasses import dataclass
from decimal import Decimal


# Параметры генерации (LE-4 v2). Идентификаторы справочников MS-2 хранятся как есть:
# проверка существования и лимиты — в SMAI-87, подстановка в промпт — в SMAI-89.
@dataclass(frozen=True)
class GenerationParams:
    people_count: int
    weekly_budget: Decimal
    habits: tuple[int, ...] = ()
    diet: int | None = None
    allergies: tuple[int, ...] = ()
    kitchen_tools: tuple[int, ...] = ()

    # Повторы в выборе схлопываются, порядок по возрастанию ID (в БД порядка нет).
    def __post_init__(self) -> None:
        for name in ("habits", "allergies", "kitchen_tools"):
            object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))

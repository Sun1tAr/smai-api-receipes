import re
from collections.abc import Callable
from decimal import Decimal

from receipes.domain import GenerationParameters, InvalidPromptTemplate

PEOPLE_COUNT = "{people_count}"
WEEKLY_BUDGET = "{weekly_budget}"
PLACEHOLDERS = (PEOPLE_COUNT, WEEKLY_BUDGET)

# Любая «{имя}» из строчных латинских букв и подчёркиваний считается плейсхолдером.
# Фигурные скобки JSON ({"type": …}, {}) под шаблон не подходят: в них кавычки.
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
_CENT = Decimal("0.01")


# Фиксированная запись без научной нотации и с двумя знаками: 1000 → «1000.00».
def format_budget(value: Decimal) -> str:
    return format(value.quantize(_CENT), "f")


# Явная замена двух плейсхолдеров, а не str.format: JSON в тексте шаблона не
# экранируется. Параметры уже проверены GenerationParameters и скобок не содержат.
# Шаблон с недостающим или неизвестным плейсхолдером — DomainError, поэтому
# claim_next переведёт такую задачу в failed.
def build_prompt(template: str, parameters: GenerationParameters) -> str:
    for placeholder in PLACEHOLDERS:
        if placeholder not in template:
            raise InvalidPromptTemplate(f"placeholder {placeholder} is missing")
    unknown = sorted(set(_PLACEHOLDER.findall(template)) - set(PLACEHOLDERS))
    if unknown:
        raise InvalidPromptTemplate(f"unknown placeholder {unknown[0]}")
    return template.replace(PEOPLE_COUNT, str(parameters.people_count)).replace(
        WEEKLY_BUDGET, format_budget(parameters.weekly_budget)
    )


# Готовый build_prompt для TaskRepository.claim_next.
def prompt_builder(template: str) -> Callable[[GenerationParameters], str]:
    return lambda parameters: build_prompt(template, parameters)

import re
from decimal import Decimal

from receipes.domain.errors import PromptTemplateError

PLACEHOLDERS = frozenset({"people_count", "weekly_budget"})
_ANY_BRACES = re.compile(r"\{([^{}]*)\}")
_PLACEHOLDER = re.compile(r"\{(people_count|weekly_budget)\}")


# Число для промпта без хвостовых нулей: 5000, 5000.5 (не 5000.00 и не 5E+3).
def format_number(value: Decimal | int) -> str:
    return format(Decimal(value).normalize(), "f")


# В шаблоне должны быть оба плейсхолдера и никаких других {…}.
def validate_template(template: str) -> None:
    found = set(_ANY_BRACES.findall(template))
    if found != PLACEHOLDERS:
        raise PromptTemplateError


# Один проход подстановки: значения не перепроверяются на {…} и не ломают формат.
def render_prompt(template: str, people_count: int, weekly_budget: Decimal) -> str:
    validate_template(template)
    values = {
        "people_count": format_number(people_count),
        "weekly_budget": format_number(weekly_budget),
    }
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)

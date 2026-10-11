import re
from collections.abc import Callable
from decimal import Decimal

from receipes.domain import GenerationParameters, InvalidPromptTemplate

PEOPLE_COUNT = "{people_count}"
WEEKLY_BUDGET = "{weekly_budget}"
PLACEHOLDERS = (PEOPLE_COUNT, WEEKLY_BUDGET)

# Плейсхолдер — «{имя}» из латинских букв, цифр и «_» (без цифры в начале); так же
# распознаются {People} и {x1}. Фигурные скобки JSON ({"type": …}, {}) под шаблон не
# подходят: в них кавычки или пробелы.
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
# {{имя}} — экранирование в стиле str.format; его здесь нет, поэтому оно отвергается,
# а не превращается молча в «{2}».
_ESCAPED = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}")
_CENT = Decimal("0.01")


# Фиксированная запись без научной нотации и с двумя знаками: 1000 → «1000.00».
def format_budget(value: Decimal) -> str:
    return format(value.quantize(_CENT), "f")


# Проверка шаблона как такового (не задачи): оба плейсхолдера на месте, неизвестных и
# экранированных нет. Дефект шаблона — не вина задачи, поэтому проверяется один раз
# при получении шаблона (PromptTemplateRepository.builder), а не при каждом claim.
def validate_template(template: str) -> None:
    for placeholder in PLACEHOLDERS:
        if placeholder not in template:
            raise InvalidPromptTemplate(f"placeholder {placeholder} is missing")
    if _ESCAPED.search(template):
        raise InvalidPromptTemplate("escaped braces {{…}} are not supported")
    unknown = sorted(set(_PLACEHOLDER.findall(template)) - set(PLACEHOLDERS))
    if unknown:
        raise InvalidPromptTemplate(f"unknown placeholder {unknown[0]}")


# Явная замена двух плейсхолдеров, а не str.format: JSON в тексте шаблона не
# экранируется. Параметры уже проверены GenerationParameters и скобок не содержат.
def build_prompt(template: str, parameters: GenerationParameters) -> str:
    validate_template(template)
    return template.replace(PEOPLE_COUNT, str(parameters.people_count)).replace(
        WEEKLY_BUDGET, format_budget(parameters.weekly_budget)
    )


# Готовый build_prompt для TaskRepository.claim_next; шаблон проверяется сразу.
def prompt_builder(template: str) -> Callable[[GenerationParameters], str]:
    validate_template(template)
    return lambda parameters: build_prompt(template, parameters)

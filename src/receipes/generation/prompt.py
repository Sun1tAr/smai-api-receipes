import re
from decimal import Decimal

from receipes.dictionaries import Dictionaries
from receipes.domain.errors import PromptTemplateError, UnknownDictionaryId
from receipes.domain.params import GenerationParams

PLACEHOLDERS = frozenset({"people_count", "weekly_budget"})
_ANY_BRACES = re.compile(r"\{([^{}]*)\}")
_PLACEHOLDER = re.compile(r"\{(people_count|weekly_budget)\}")


# Число для промпта без хвостовых нулей: 5000, 5000.5 (не 5000.00 и не 5E+3).
def format_number(value: Decimal | int) -> str:
    return format(Decimal(value).normalize(), "f")


# В шаблоне должны быть оба плейсхолдера и никаких других скобок: ни других {…},
# ни «{{», «}}» (экранирование здесь не поддерживается), ни непарных { и }.
def validate_template(template: str) -> None:
    found = set(_ANY_BRACES.findall(template))
    leftover = _ANY_BRACES.sub("", template)
    if found != PLACEHOLDERS or "{" in leftover or "}" in leftover:
        raise PromptTemplateError


# Название справочника в промпт: пробелы схлопнуты, в «ёлочках», чтобы запятые и «;»
# внутри названия не склеивались с разделителями списка.
def _quote(name: str) -> str:
    return "«" + " ".join(name.split()) + "»"


def _names(known: dict[int, str], ids: tuple[int, ...]) -> str:
    try:
        return ", ".join(_quote(known[value]) for value in ids)
    except KeyError:
        # Значение ID (пользовательские данные) в сообщение не попадает.
        raise UnknownDictionaryId from None


def needs_dictionaries(params: GenerationParams) -> bool:
    return bool(
        params.habits
        or params.diet is not None
        or params.allergies
        or params.kitchen_tools
    )


# Блок ограничений v2 (SMAI-89). Пустые списки и diet=None строк не дают; если
# выбрано всё пусто, блок пустой. Порядок строк и ID фиксирован (ID по возрастанию),
# поэтому одинаковые параметры дают побайтно одинаковый блок.
def build_constraints(params: GenerationParams, dictionaries: Dictionaries) -> str:
    lines: list[str] = []
    if params.habits:
        lines.append("привычки питания: " + _names(dictionaries.habits, params.habits))
    if params.diet is not None:
        lines.append("диета: " + _names(dictionaries.diets, (params.diet,)))
    if params.allergies:
        lines.append(
            "аллергены (не использовать ни в одном блюде и ингредиенте): "
            + _names(dictionaries.allergies, params.allergies)
        )
    if params.kitchen_tools:
        lines.append(
            "доступная кухонная техника (не требуй другой): "
            + _names(dictionaries.kitchen_tools, params.kitchen_tools)
        )
    if not lines:
        return ""
    body = ";\n".join(f"- {line}" for line in lines) + "."
    return "Ограничения пользователя (обязательны к выполнению):\n" + body


# Блок ставится перед последним абзацем шаблона («Верни только JSON…»), а если
# абзацев нет, то в конец. Хвостовые пробелы шаблона отбрасываются.
def _insert_constraints(text: str, constraints: str) -> str:
    if not constraints:
        return text
    text = text.rstrip()
    head, separator, last = text.rpartition("\n\n")
    if not separator:
        return f"{text}\n\n{constraints}"
    return f"{head}\n\n{constraints}\n\n{last}"


# Один проход подстановки: значения не перепроверяются на {…} и не ломают формат.
# Блок ограничений вставляется уже после подстановки: названия из справочников не
# проходят через шаблон.
def render_prompt(
    template: str,
    people_count: int,
    weekly_budget: Decimal,
    constraints: str = "",
) -> str:
    validate_template(template)
    values = {
        "people_count": format_number(people_count),
        "weekly_budget": format_number(weekly_budget),
    }
    text = _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)
    return _insert_constraints(text, constraints)

import re
import unicodedata

_SPACES = re.compile(r"\s+")
_QUOTES = str.maketrans("", "", "«»„“”\"'‘’")


# Название для хранения и выдачи: NFKC, схлопнутые пробелы (включая NBSP), strip.
def normalize_name(value: str) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", value)).strip()


# Ключ только для сравнения названий (дубли блюд и ингредиентов), в данные не попадает.
def name_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    text = _SPACES.sub(" ", text.translate(_QUOTES)).strip()
    return text.rstrip(".,;:!…").strip()

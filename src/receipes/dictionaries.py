import asyncio
from collections.abc import Iterable
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from receipes.config import Settings
from receipes.domain.errors import DictionaryError

DICTIONARIES_PATH = "/dictionaries"
UNKNOWN_ID = "unknown_id"


class _Item(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: StrictInt
    name: str


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    habits: list[_Item]
    diets: list[_Item]
    allergies: list[_Item]
    kitchenTools: list[_Item]  # noqa: N815 - имя поля из контракта API-7


# Справочники MS-2: ID -> название.
@dataclass(frozen=True)
class Dictionaries:
    habits: dict[int, str]
    diets: dict[int, str]
    allergies: dict[int, str]
    kitchen_tools: dict[int, str]


# Справочник MS-2 не бывает пустым и не повторяет id: иначе это сбой сида MS-2
# (вина сервера), а не «неизвестный ID» от клиента.
def _names(items: list[_Item]) -> dict[int, str]:
    names = {item.id: item.name for item in items}
    if not names or len(names) != len(items):
        raise DictionaryError("Invalid dictionaries response")
    return names


# Место в теле запроса: ["body", "habits", 1].
Location = tuple[str | int, ...]


# Неизвестные ID по исходным спискам запроса: индекс в `loc` указывает на элемент,
# который прислал клиент (в GenerationParams порядок и повторы уже изменены).
# Повторяющийся неизвестный ID даёт ошибку на каждое вхождение. Значения ID в
# результат не попадают.
def find_unknown_ids(
    dictionaries: Dictionaries,
    *,
    habits: Iterable[int],
    diet: int | None,
    allergies: Iterable[int],
    kitchen_tools: Iterable[int],
) -> list[Location]:
    def unknown_in(
        field: str, known: dict[int, str], values: Iterable[int]
    ) -> list[Location]:
        return [
            ("body", field, index)
            for index, value in enumerate(values)
            if value not in known
        ]

    # Порядок полей как в запросе: habits, diet, allergies, kitchenTools.
    unknown = unknown_in("habits", dictionaries.habits, habits)
    if diet is not None and diet not in dictionaries.diets:
        unknown.append(("body", "diet"))
    unknown += unknown_in("allergies", dictionaries.allergies, allergies)
    unknown += unknown_in("kitchenTools", dictionaries.kitchen_tools, kitchen_tools)
    return unknown


# Клиент API-7 MS-2 (`GET /dictionaries`). Ответ не кэшируется: запросы с ID редки
# (генерация меню тяжёлая), а кэш добавил бы настройку и риск, что новый ID ещё
# отклоняется.
class DictionaryClient:
    def __init__(self, http: httpx.AsyncClient, deadline_seconds: float) -> None:
        self._http = http
        self._deadline = deadline_seconds

    @staticmethod
    def build_http_client(
        settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=str(settings.ms2_base_url),
            timeout=settings.ms2_timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> "DictionaryClient":
        return cls(
            cls.build_http_client(settings, transport), settings.ms2_timeout_seconds
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self) -> Dictionaries:
        # Таймауты httpx действуют на каждую фазу отдельно: медленный ответ по
        # байту иначе держал бы запрос долго, поэтому есть общий дедлайн.
        # TimeoutError ловится снаружи блока `asyncio.timeout`.
        try:
            async with asyncio.timeout(self._deadline):
                response = await self._http.get(DICTIONARIES_PATH)
        except (httpx.TimeoutException, TimeoutError):
            raise DictionaryError("Dictionaries timeout") from None
        except httpx.HTTPError:
            raise DictionaryError("Dictionaries unavailable") from None
        if response.status_code != 200:
            raise DictionaryError("Dictionaries error")
        try:
            payload = _Payload.model_validate_json(response.content)
        except ValidationError:
            raise DictionaryError("Invalid dictionaries response") from None
        return Dictionaries(
            habits=_names(payload.habits),
            diets=_names(payload.diets),
            allergies=_names(payload.allergies),
            kitchen_tools=_names(payload.kitchenTools),
        )

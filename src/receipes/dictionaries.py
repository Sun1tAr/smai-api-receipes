from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from receipes.config import Settings
from receipes.domain.errors import DictionaryError

DICTIONARIES_PATH = "/dictionaries"


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


def _names(items: list[_Item]) -> dict[int, str]:
    return {item.id: item.name for item in items}


# Клиент API-7 MS-2 (`GET /dictionaries`). Ответ не кэшируется: справочники редко
# меняются, но решение о кэше принимает SMAI-87 вместе с проверкой ID.
class DictionaryClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @staticmethod
    def build_http_client(
        settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> httpx.AsyncClient:
        if settings.ms2_base_url is None:
            raise ValueError("MS2_BASE_URL is not set")
        return httpx.AsyncClient(
            base_url=str(settings.ms2_base_url),
            timeout=settings.ms2_timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self) -> Dictionaries:
        try:
            response = await self._http.get(DICTIONARIES_PATH)
        except httpx.TimeoutException:
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

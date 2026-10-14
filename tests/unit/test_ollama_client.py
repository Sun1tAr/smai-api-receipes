import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from fakes import unavailable_db_settings
from jsonschema import validate
from menu_samples import ollama_reply, sample_menu
from opentelemetry.trace import NoOpTracer
from prometheus_client import CollectorRegistry

from receipes.domain.errors import (
    GenerationError,
    InvalidNeurocoreResponse,
    NeurocoreError,
    NeurocoreModelNotFound,
    NeurocoreTimeout,
    NeurocoreTruncated,
    NeurocoreUnavailable,
)
from receipes.domain.menu import MenuPayload
from receipes.generation import ollama as ollama_module
from receipes.generation.metrics import GenerationMetrics
from receipes.generation.ollama import RESPONSE_FORMAT, OllamaClient

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(
    handler: Handler, **settings: object
) -> tuple[OllamaClient, CollectorRegistry]:
    config = unavailable_db_settings(**settings)
    registry = CollectorRegistry()
    http = OllamaClient.build_http_client(config, httpx.MockTransport(handler))
    client = OllamaClient(config, http, GenerationMetrics(registry), NoOpTracer())
    return client, registry


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=ollama_reply(sample_menu()))


def outcome(registry: CollectorRegistry, name: str) -> float:
    return registry.get_sample_value("ollama_requests_total", {"outcome": name})


async def test_request_follows_api3_with_settings() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(request)

    client, registry = make_client(
        handler, ollama_num_ctx=8192, ollama_num_predict=4096, ollama_temperature=0.5
    )
    generated = await client.generate_menu("промпт")
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/api/generate"
    body = json.loads(request.content)
    assert body["model"] == "qwen2.5:7b"
    assert body["prompt"] == "промпт"
    assert body["stream"] is False
    assert body["format"] == RESPONSE_FORMAT
    assert body["options"] == {"num_ctx": 8192, "num_predict": 4096, "temperature": 0.5}
    assert body["keep_alive"] == "30m"
    assert len(generated.menu.recipes) == 3
    assert (generated.prompt_eval_count, generated.eval_count) == (820, 900)
    assert outcome(registry, "success") == 1
    assert registry.get_sample_value("ollama_request_duration_seconds_count") == 1


async def test_model_name_comes_from_settings() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return ok(request)

    client, _ = make_client(handler, ollama_model="other:model")
    await client.generate_menu("x")
    assert bodies[0]["model"] == "other:model"


def test_schema_fixtures_and_sample_menu_satisfy_both_validators() -> None:
    menu = sample_menu()
    validate(menu, RESPONSE_FORMAT)
    MenuPayload.model_validate(menu)


@pytest.mark.parametrize(
    ("status", "error", "label"),
    [
        (503, NeurocoreUnavailable, "unavailable"),
        (404, NeurocoreModelNotFound, "error"),
        (400, NeurocoreError, "error"),
        (500, NeurocoreError, "error"),
        (502, NeurocoreError, "error"),
    ],
)
async def test_http_errors_map_to_safe_messages(
    status: int, error: type[GenerationError], label: str
) -> None:
    client, registry = make_client(lambda r: httpx.Response(status, text="secret body"))
    with pytest.raises(error) as raised:
        await client.generate_menu("x")
    assert "secret" not in raised.value.message
    assert outcome(registry, label) == 1


async def test_connection_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client, registry = make_client(handler)
    with pytest.raises(NeurocoreUnavailable) as raised:
        await client.generate_menu("x")
    assert raised.value.message == "Neurocore unavailable"
    assert outcome(registry, "unavailable") == 1


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError])
async def test_broken_connection_is_unavailable(error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error("broken")

    client, _ = make_client(handler)
    with pytest.raises(NeurocoreUnavailable):
        await client.generate_menu("x")


async def test_http_read_timeout_is_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client, registry = make_client(handler)
    with pytest.raises(NeurocoreTimeout) as raised:
        await client.generate_menu("x")
    assert raised.value.message == "Neurocore timeout"
    assert outcome(registry, "timeout") == 1


async def test_overall_deadline_caps_the_whole_call() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return ok(request)

    client, registry = make_client(handler, ollama_timeout_seconds=0.05)
    with pytest.raises(NeurocoreTimeout):
        await client.generate_menu("x")
    assert outcome(registry, "timeout") == 1


async def test_truncated_response_is_not_parsed_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForbiddenJson:
        @staticmethod
        def loads(*args: object, **kwargs: object) -> object:
            raise AssertionError("json.loads must not be called")

    monkeypatch.setattr(ollama_module, "json", ForbiddenJson)
    reply = ollama_reply(
        '{"recipes": [{"name": "Суп',
        done_reason="length",
        prompt_eval_count=810,
        eval_count=4096,
    )
    client, registry = make_client(lambda r: httpx.Response(200, json=reply))
    with pytest.raises(NeurocoreTruncated) as raised:
        await client.generate_menu("x")
    assert raised.value.message == "Neurocore response truncated"
    # Счётчики токенов доходят до логов отказа: при обрезке eval_count == лимит.
    assert (raised.value.prompt_eval_count, raised.value.eval_count) == (810, 4096)
    assert outcome(registry, "truncated") == 1
    assert outcome(registry, "invalid_response") == 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"done": True}),
        httpx.Response(200, json=ollama_reply("not json at all")),
        httpx.Response(200, json=ollama_reply("[1, 2]")),
        httpx.Response(200, json=ollama_reply({"recipes": []})),
        httpx.Response(200, json=ollama_reply('{"recipes": [NaN]}')),
    ],
)
async def test_unparsable_or_invalid_answers_are_invalid_response(
    response: httpx.Response,
) -> None:
    client, registry = make_client(lambda r: response)
    with pytest.raises(InvalidNeurocoreResponse) as raised:
        await client.generate_menu("x")
    assert raised.value.message == "Invalid neurocore response"
    assert outcome(registry, "invalid_response") == 1


async def test_rejection_carries_a_reason_for_metrics() -> None:
    menu = sample_menu()
    menu["recipes"] = menu["recipes"][:2]
    client, _ = make_client(lambda r: httpx.Response(200, json=ollama_reply(menu)))
    with pytest.raises(InvalidNeurocoreResponse) as raised:
        await client.generate_menu("x")
    assert raised.value.reason == "count"
    assert (raised.value.prompt_eval_count, raised.value.eval_count) == (820, 900)


async def test_missing_token_counters_are_none() -> None:
    reply = ollama_reply(sample_menu(), prompt_eval_count=None, eval_count=None)
    client, _ = make_client(lambda r: httpx.Response(200, json=reply))
    generated = await client.generate_menu("x")
    assert (generated.prompt_eval_count, generated.eval_count) == (None, None)


async def test_client_can_be_closed() -> None:
    client, _ = make_client(ok)
    await client.aclose()


async def test_huge_number_is_invalid_response_with_bounds_reason() -> None:
    menu = sample_menu()
    menu["recipes"][0]["calories"] = 10**40
    reply = ollama_reply(json.dumps(menu))
    client, registry = make_client(lambda r: httpx.Response(200, json=reply))
    with pytest.raises(InvalidNeurocoreResponse) as raised:
        await client.generate_menu("x")
    assert raised.value.reason == "bounds"
    assert outcome(registry, "invalid_response") == 1


async def test_cancelled_call_is_not_counted_as_an_error() -> None:
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return ok(request)

    client, registry = make_client(handler)
    running = asyncio.create_task(client.generate_menu("x"))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    for name in (
        "success",
        "timeout",
        "unavailable",
        "invalid_response",
        "truncated",
        "error",
    ):
        assert outcome(registry, name) == 0
    assert registry.get_sample_value("ollama_request_duration_seconds_count") == 0


@pytest.mark.parametrize(
    ("result", "expected"),
    [(httpx.Response(200, json={"models": []}), True), (httpx.Response(503), False)],
)
async def test_availability_probe_uses_tags_endpoint(
    result: httpx.Response, expected: bool
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return result

    client, registry = make_client(handler)
    assert await client.is_available() is expected
    assert (seen[0].method, seen[0].url.path) == ("GET", "/api/tags")
    # Проба не считается запросом генерации.
    assert registry.get_sample_value("ollama_request_duration_seconds_count") == 0


async def test_availability_probe_survives_connection_errors_and_hangs() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client, _ = make_client(refused)
    assert await client.is_available() is False

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return ok(request)

    slow, _ = make_client(hang, ollama_connect_timeout_seconds=0.05)
    assert await slow.is_available() is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"models": [{"name": "qwen2.5:7b"}]}, True),
        ({"models": [{"name": "other:1b"}, {"name": "qwen2.5:7b"}]}, True),
        ({"models": [{"name": "qwen2.5:7b:latest"}]}, True),
        ({"models": [{"name": "qwen2.5:7b-instruct-q4_K_M"}]}, False),
        ({"models": []}, False),
        ({"unexpected": 1}, False),
        ({"models": [{"model": "no-name"}]}, False),
    ],
)
async def test_model_presence_is_checked_in_tags(payload: dict, expected: bool) -> None:
    client, _ = make_client(lambda r: httpx.Response(200, json=payload))
    assert await client.is_available(require_model=True) is expected
    # Без require_model годится любой ответ 200.
    assert await client.is_available() is True


async def test_tags_that_are_not_json_do_not_pass_the_model_check() -> None:
    client, _ = make_client(lambda r: httpx.Response(200, text="<html>"))
    assert await client.is_available(require_model=True) is False

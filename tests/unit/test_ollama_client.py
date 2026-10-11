import asyncio
import json
from typing import Any

import httpx
import pytest

from receipes.ollama import (
    SAFE_MESSAGES,
    FailureReason,
    GenerationFailure,
    OllamaClient,
)
from tests.unit.fakes import unavailable_db_settings
from tests.unit.test_generated_menu import payload


def client_for(handler, **settings: Any) -> OllamaClient:
    return OllamaClient(
        unavailable_db_settings(**settings), transport=httpx.MockTransport(handler)
    )


def ok(body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"response": json.dumps(body or payload())})


async def failure_of(client: OllamaClient) -> GenerationFailure:
    with pytest.raises(GenerationFailure) as error:
        await client.generate_menu("prompt")
    await client.close()
    return error.value


async def test_success_returns_the_gated_menu_and_sends_the_expected_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok()

    client = client_for(handler, ollama_model="test-model", ollama_num_predict=777)
    menu = await client.generate_menu("PROMPT TEXT")
    await client.close()
    assert len(menu.recipes) == 3
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/api/generate"
    body = json.loads(request.content)
    assert body["model"] == "test-model" and body["prompt"] == "PROMPT TEXT"
    assert body["stream"] is False
    assert body["format"]["required"] == ["recipes"]
    assert body["options"]["num_predict"] == 777
    assert {"temperature", "num_ctx"} <= set(body["options"])
    assert body["keep_alive"]


@pytest.mark.parametrize("status", [500, 503, 404, 400])
async def test_http_errors_fail_with_a_safe_message(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="model said: secret-model-output")

    failure = await failure_of(client_for(handler))
    assert failure.reason is FailureReason.HTTP_ERROR
    assert not failure.retryable
    assert "secret" not in failure.message and "secret" not in str(failure)


@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (httpx.ConnectError("refused"), FailureReason.UNAVAILABLE),
        (httpx.ConnectTimeout("connect"), FailureReason.UNAVAILABLE),
        (httpx.ReadTimeout("read"), FailureReason.TIMEOUT),
        (httpx.WriteTimeout("write"), FailureReason.TIMEOUT),
        (httpx.RemoteProtocolError("broken"), FailureReason.UNAVAILABLE),
        (httpx.ReadError("reset"), FailureReason.UNAVAILABLE),
    ],
)
async def test_transport_errors_are_mapped_and_never_retried(
    exception: Exception, reason: FailureReason
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    failure = await failure_of(client_for(handler))
    assert failure.reason is reason and not failure.retryable


async def test_the_total_timeout_covers_the_whole_call() -> None:
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await never.wait()
        return ok()

    failure = await failure_of(client_for(handler, ollama_timeout_seconds=0.05))
    assert failure.reason is FailureReason.TIMEOUT


async def test_truncated_response_has_its_own_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"response": '{"recipes": [', "done_reason": "length"}
        )

    failure = await failure_of(client_for(handler))
    assert failure.reason is FailureReason.TRUNCATED
    assert "token limit" in failure.message and not failure.retryable


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"response": "not json at all"}),
        httpx.Response(200, json={"response": '{"recipes": []}'}),
        httpx.Response(200, json={"response": "[1, 2]"}),
        httpx.Response(200, json={"other": "field"}),
        httpx.Response(200, json=["not", "a", "dict"]),
        httpx.Response(200, text="<html>proxy error</html>"),
        httpx.Response(200, json={"response": None}),
    ],
)
async def test_invalid_responses_are_retryable_and_leak_nothing(response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    failure = await failure_of(client_for(handler))
    assert failure.reason is FailureReason.INVALID_RESPONSE and failure.retryable
    assert failure.message == SAFE_MESSAGES[FailureReason.INVALID_RESPONSE]


async def test_gate_rejection_does_not_log_the_model_output(capsys) -> None:
    from receipes.observability import configure_logging

    configure_logging("INFO")
    bad = payload()
    bad["recipes"][0]["calories"] = -1
    bad["recipes"][0]["name"] = "SECRET-DISH-NAME"

    def handler(request: httpx.Request) -> httpx.Response:
        return ok(bad)

    failure = await failure_of(client_for(handler))
    assert failure.reason is FailureReason.INVALID_RESPONSE
    output = capsys.readouterr().out
    assert "SECRET-DISH-NAME" not in output
    assert "ollama_invalid_response" in output


def test_every_reason_has_a_short_safe_message() -> None:
    assert set(SAFE_MESSAGES) == set(FailureReason)
    assert all(0 < len(message) < 80 for message in SAFE_MESSAGES.values())


async def test_probe_reports_availability_without_a_model_call() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"models": []})

    client = client_for(handler)
    assert await client.probe() is True
    await client.close()
    assert seen == ["/api/tags"]


@pytest.mark.parametrize(
    "outcome",
    [httpx.ConnectError("x"), httpx.ReadTimeout("y"), httpx.Response(503)],
)
async def test_probe_is_false_when_ollama_is_down(outcome) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client = client_for(handler)
    assert await client.probe() is False
    await client.close()


async def test_probe_is_bounded_by_the_connect_timeout() -> None:
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await never.wait()
        return httpx.Response(200)

    client = client_for(handler, ollama_connect_timeout_seconds=0.05)
    assert await client.probe() is False
    await client.close()


@pytest.mark.parametrize("depth", [2000, 100_000])
async def test_deeply_nested_model_output_is_an_invalid_response(depth) -> None:
    nested = "[" * depth + "]" * depth

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": nested})

    failure = await failure_of(client_for(handler))
    assert failure.reason is FailureReason.INVALID_RESPONSE and failure.retryable


async def test_deeply_nested_envelope_is_an_invalid_response() -> None:
    envelope = "[" * 100_000 + "]" * 100_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=envelope.encode())

    failure = await failure_of(client_for(handler))
    assert failure.reason is FailureReason.INVALID_RESPONSE

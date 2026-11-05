import asyncio
import json

import httpx
import pytest
from fakes import unavailable_db_settings
from pydantic import AnyHttpUrl

from receipes.main import create_app

_PATH = "/recipes/cart/link"
_ITEMS = [{"id": 606, "q": 0.01}, {"id": 999_999_999, "q": 40}]
_LINK = "https://www.vkusvill.ru/cart?products=606%3A0.01&signature=a%2Bb"


def rpc_result(request, result, **kwargs):
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": request["id"], "result": result},
        **kwargs,
    )


def tool_result(request, payload):
    return rpc_result(
        request,
        {
            "isError": not payload["ok"],
            "content": [{"type": "text", "text": json.dumps(payload)}],
        },
    )


@pytest.fixture
async def make_client():
    apps = []
    clients = []

    def make(reply=None, *, configured=True, **settings):
        seen = []

        async def handle(request):
            body = json.loads(request.content)
            seen.append(body)
            match body["method"]:
                case "initialize":
                    return rpc_result(
                        body,
                        {"protocolVersion": "2025-03-26"},
                        headers={"Mcp-Session-Id": "session-1"},
                    )
                case "notifications/initialized":
                    return httpx.Response(202)
                case "tools/call":
                    assert request.headers["Mcp-Session-Id"] == "session-1"
                    return await reply(body)
            raise AssertionError(body)

        app = create_app(
            unavailable_db_settings(
                vkusvill_mcp_url=AnyHttpUrl("https://mcp.test/mcp")
                if configured
                else None,
                **settings,
            ),
            mcp_transport=httpx.MockTransport(handle),
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        apps.append(app)
        clients.append(client)
        return client, seen, app.state.vkusvill_mcp

    yield make
    for client in clients:
        await client.aclose()
    for app in apps:
        if app.state.vkusvill_mcp is not None:
            await app.state.vkusvill_mcp.aclose()
        await app.state.ollama.aclose()
        await app.state.dictionaries.aclose()
        await app.state.engine.dispose()
        app.state.tracer_provider.shutdown()


async def test_link_sends_all_items_once_per_request_and_preserves_url(make_client):
    async def reply(body):
        return tool_result(body, {"ok": True, "data": {"link": _LINK}})

    client, seen, _ = make_client(reply)
    for _ in range(2):
        response = await client.post(_PATH, json={"items": _ITEMS})
        assert response.status_code == 200
        assert response.json() == {"link": _LINK}
    assert [body["method"] for body in seen] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
    ]
    for body in seen[2:]:
        assert body["params"] == {
            "name": "vkusvill_cart_link_create",
            "arguments": {"products": _ITEMS},
        }
        assert type(body["params"]["arguments"]["products"][0]["q"]) is float


@pytest.mark.parametrize(
    "items",
    [
        None,
        [],
        _ITEMS[:1] * 21,
        *[
            [_ITEMS[0], item]
            for item in (
                {"q": 1},
                {"id": 606},
                {"id": "606", "q": 1},
                {"id": True, "q": 1},
                {"id": 606.0, "q": 1},
                {"id": 0, "q": 1},
                {"id": 1_000_000_000, "q": 1},
                {"id": 606, "q": "1"},
                {"id": 606, "q": True},
                {"id": 606, "q": 0.009},
                {"id": 606, "q": 40.01},
            )
        ],
    ],
)
async def test_rejects_entire_request_before_calling_provider(make_client, items):
    client, seen, _ = make_client()
    response = await client.post(_PATH, json={"items": items})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert seen == []


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999"])
async def test_rejects_non_finite_quantity(make_client, value):
    client, seen, _ = make_client()
    response = await client.post(
        _PATH,
        content='{"items":[{"id":606,"q":' + value + "}]}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert seen == []


@pytest.mark.parametrize(
    "link",
    [
        None,
        42,
        "",
        "//vkusvill.ru/cart",
        "http://vkusvill.ru/cart",
        "https://vkusvill.ru.evil.test/cart",
        "https://vkusvill.ru@evil.test/cart",
        "https://@vkusvill.ru/cart",
        "https://evil.test\\@vkusvill.ru/cart",
        "https://vkusvill.ru:99999/cart",
        r"https://vkusvill.ru/cart\payload",
        "https://vkusvill.ru/cart/\ud800",
        " https://vkusvill.ru/cart",
        "https://vkusvill.ru/ca\trt",
        "https://vkusvill.ru/ca\x7frt",
    ],
)
async def test_rejects_invalid_provider_link(make_client, link):
    async def reply(body):
        return tool_result(body, {"ok": True, "data": {"link": link}})

    client, seen, _ = make_client(reply)
    response = await client.post(_PATH, json={"items": _ITEMS})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EXTERNAL_SERVICE_ERROR"
    assert len(seen) == 3


@pytest.mark.parametrize(
    "code,status,http_status,expected",
    [
        ("invalid_input", 400, 200, 400),
        ("different_error", 400, 200, 503),
        ("invalid_input", 422, 200, 503),
        ("invalid_input", "400", 200, 503),
        ("invalid_input", 400, 400, 503),
        ("invalid_input", 400, 404, 503),
        ("invalid_input", 400, 503, 503),
    ],
)
async def test_only_typed_business_refusal_is_400(
    make_client, code, status, http_status, expected
):
    async def reply(body):
        if http_status != 200:
            return httpx.Response(http_status, content="provider secret")
        return tool_result(
            body,
            {
                "ok": False,
                "error": {
                    "code": code,
                    "http_status": status,
                    "message": "provider secret",
                },
            },
        )

    client, seen, _ = make_client(reply)
    response = await client.post(_PATH, json={"items": _ITEMS})
    assert response.status_code == expected
    error = response.json()["error"]
    assert error["code"] == (
        "VALIDATION_ERROR" if expected == 400 else "EXTERNAL_SERVICE_ERROR"
    )
    assert error["details"] == {}
    assert "provider secret" not in response.text
    assert len(seen) == 3


async def test_indeterminate_network_failure_is_never_retried(make_client):
    async def reply(body):
        raise httpx.ReadError("provider secret")

    client, seen, _ = make_client(reply, mcp_call_retries=1)
    response = await client.post(_PATH, json={"items": _ITEMS})
    assert response.status_code == 503
    assert len(seen) == 3


async def test_outer_deadline_includes_wait_for_shared_transport_slot(make_client):
    started = asyncio.Event()

    async def reply(body):
        started.set()
        await asyncio.Event().wait()

    client, seen, mcp = make_client(
        reply, cart_deadline_seconds=0.05, mcp_concurrency=1
    )
    async with asyncio.timeout(1):
        search = asyncio.create_task(
            mcp.call("vkusvill_products_search", {"q": "морковь"})
        )
        try:
            await started.wait()
            response = await client.post(_PATH, json={"items": _ITEMS})
            assert response.status_code == 503
            assert [body["params"]["name"] for body in seen[2:]] == [
                "vkusvill_products_search"
            ]
        finally:
            search.cancel()
            with pytest.raises(asyncio.CancelledError):
                await search


async def test_missing_provider_is_503(make_client):
    client, seen, _ = make_client(configured=False)
    response = await client.post(_PATH, json={"items": _ITEMS})
    assert response.status_code == 503
    assert seen == []

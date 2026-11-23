import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from receipes.cart import mcp
from receipes.cart.mcp import CART_LINK_TOOL, McpClient, McpError

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mcp"


def _capture(name: str) -> dict:
    return json.loads((_FIXTURES / f"{name}.json").read_bytes())


def _rpc(request: dict, result: dict, **kwargs) -> httpx.Response:
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": request["id"], "result": result}, **kwargs
    )


def _data(request: dict, data: dict) -> httpx.Response:
    return _rpc(
        request,
        {"content": [{"type": "text", "text": json.dumps({"ok": True, "data": data})}]},
    )


def _initialize(request: dict) -> httpx.Response | None:
    if request["method"] == "initialize":
        return _rpc(request, _capture("init")["result"])
    if request["method"] == "notifications/initialized":
        return httpx.Response(202)
    return None


@pytest.fixture
async def make_client() -> AsyncIterator:
    clients = []

    def make(handler, **kwargs) -> McpClient:
        client = McpClient(
            "https://mcp.test/mcp", transport=httpx.MockTransport(handler), **kwargs
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


@pytest.mark.parametrize("session_id", [None, "session-1"])
async def test_handshake_and_live_catalog_envelopes(make_client, session_id) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mcp"
        assert request.method == "POST"
        assert set(request.headers["Accept"].split(", ")) == {
            "application/json",
            "text/event-stream",
        }
        body = json.loads(request.content)
        assert body["jsonrpc"] == "2.0"
        seen.append(body["method"])
        match body["method"]:
            case "initialize":
                assert "Mcp-Session-Id" not in request.headers
                assert body["params"]["protocolVersion"] == "2025-03-26"
                assert body["id"] == 1
                return httpx.Response(
                    200,
                    json=_capture("init"),
                    headers={"Mcp-Session-Id": session_id} if session_id else {},
                )
            case "notifications/initialized":
                assert "id" not in body
                assert request.headers.get("Mcp-Session-Id") == session_id
                assert request.headers["MCP-Protocol-Version"] == "2025-03-26"
                return httpx.Response(202)
            case "tools/call":
                assert request.headers.get("Mcp-Session-Id") == session_id
                assert request.headers["MCP-Protocol-Version"] == "2025-03-26"
                if body["params"]["name"] == "vkusvill_products_search":
                    assert body["params"]["arguments"] == {"q": "Молоко"}
                    assert body["id"] == 2
                    return httpx.Response(200, json=_capture("search"))
                assert body["params"] == {
                    "name": "vkusvill_product_details",
                    "arguments": {"id": 72194},
                }
                capture = _capture("details-milk")
                capture["id"] = 3
                assert body["id"] == 3
                return httpx.Response(200, json=capture)
        raise AssertionError(body)

    client = make_client(handler)
    assert seen == []
    search = await client.call("vkusvill_products_search", {"q": "Молоко"})
    assert search["items"][0]["id"] == 36296
    assert search["items"][0]["weight"]["value"] == Decimal("0.90000000000000002")
    details = await client.call("vkusvill_product_details", {"id": 72194})
    assert details["id"] == 72194
    assert details["weight"]["value"] == Decimal("1.3999999999999999")
    assert seen == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
    ]


async def test_sse_closes_after_matching_multiline_event(make_client) -> None:
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"\xef"
            yield b"\xbb\xbf: heartbeat\r"
            yield b'\ndata: {"jsonrpc":"2.0","id":77,"result":{}}\r\n\r'
            yield b'\nevent: message\rdata: {"jsonrpc":"2.0",\r\ndata: "id":2,'
            event = (
                ' "result":{"content":[{"type":"text","text":"'
                r"{\"ok\": true,\"data\":{\"name\":\"молоко\"}}"
                '"}]}}\r\n\r'
            ).encode()
            split = event.index("м".encode()) + 1
            yield event[:split]
            yield event[split:]
            raise AssertionError("read past the matching SSE event")

        async def aclose(self) -> None:
            self.closed = True

    stream = Stream()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return _initialize(body) or httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
            stream=stream,
        )

    client = make_client(handler)
    assert await client.call("search", {}) == {"name": "молоко"}
    assert stream.closed


@pytest.mark.parametrize(
    "failure,unavailable,retryable,status,calls",
    [
        ("wrong id", True, False, None, 1),
        ("rpc error", False, False, None, 1),
        ("rpc malformed", True, False, None, 1),
        ("rpc transient", True, True, 503, 2),
        ("isError", False, False, None, 1),
        ("malformed content", True, False, None, 1),
        ("http 401", True, False, 401, 1),
        ("http 429", True, True, 429, 2),
        ("http 502", True, True, 502, 2),
        ("logical 400", False, False, 400, 1),
        ("logical 503 false", True, False, 503, 1),
        ("logical 503 true", True, True, 503, 2),
        ("logical 503 absent", True, True, 503, 2),
        ("logical 999 absent", False, False, None, 1),
    ],
)
async def test_errors_are_classified_retried_and_not_cached(
    make_client, failure, unavailable, retryable, status, calls
) -> None:
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if (response := _initialize(body)) is not None:
            return response
        attempts.append(body["id"])
        if len(attempts) > calls:
            return _data(body, {"items": []})
        match failure.split():
            case ["wrong", "id"]:
                return _data({"id": body["id"] + 1}, {"items": []})
            case ["rpc", kind]:
                error = {"code": -32602, "message": "invalid arguments"}
                if kind == "malformed":
                    error = {}
                elif kind == "transient":
                    error["data"] = {"error": {"http_status": 503, "retryable": True}}
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": error,
                    },
                )
            case ["isError"]:
                return _rpc(
                    body,
                    {"isError": True, "content": [{"type": "text", "text": "error"}]},
                )
            case ["malformed", "content"]:
                return _rpc(body, {"content": [{"type": "text", "text": "not JSON"}]})
            case ["http", code]:
                return httpx.Response(int(code))
            case ["logical", code, *flag]:
                error = {"http_status": int(code)}
                if flag != ["absent"]:
                    error["retryable"] = retryable
                result = {"ok": False, "error": error}
                return _rpc(
                    body,
                    {
                        "isError": True,
                        "content": [{"type": "text", "text": json.dumps(result)}],
                    },
                )
        raise AssertionError(failure)

    client = make_client(handler)
    with pytest.raises(McpError) as caught:
        await client.call("search", {})
    assert caught.value.unavailable is unavailable
    assert caught.value.retryable is retryable
    assert caught.value.http_status == status
    assert len(attempts) == len(set(attempts)) == calls
    assert await client.call("search", {}) == {"items": []}


async def test_cache_uses_tool_and_canonical_arguments_with_ttl_and_size_bound(
    make_client, monkeypatch
) -> None:
    now = 1000.0
    monkeypatch.setattr(mcp, "monotonic", lambda: now)
    monkeypatch.setattr(mcp, "_CACHE_LIMIT", 2)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        body = json.loads(request.content)
        if (response := _initialize(body)) is not None:
            return response
        attempts += 1
        return _data(body, {"items": []})

    client = make_client(handler)
    result = await client.call("search", {"q": "молоко", "limit": 10})
    result["items"].append("local modification")
    now += 899
    assert await client.call("search", {"limit": 10, "q": "молоко"}) == {"items": []}
    assert attempts == 1
    now += 1
    assert await client.call("search", {"limit": 10, "q": "молоко"}) == {"items": []}
    assert attempts == 2
    await client.call("details", {"limit": 10, "q": "молоко"})
    await client.call("search", {"q": "яйцо"})
    await client.call("search", {"q": "молоко", "limit": 10})
    assert attempts == 5


async def test_each_attempt_times_out_and_retries(make_client) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        body = json.loads(request.content)
        if (response := _initialize(body)) is not None:
            return response
        attempts += 1
        if attempts == 1:
            await asyncio.Event().wait()
        return _data(body, {"items": []})

    client = make_client(handler, timeout=0.02)
    async with asyncio.timeout(1):
        assert await client.call("search", {}) == {"items": []}
    assert attempts == 2


async def test_concurrent_calls_share_four_slots_and_cancel_without_leaking(
    make_client,
) -> None:
    entered = asyncio.Queue()
    release = asyncio.Event()
    active = 0
    peak = 0
    initialize_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak, initialize_calls
        body = json.loads(request.content)
        if body["method"] == "initialize":
            initialize_calls += 1
        if (response := _initialize(body)) is not None:
            return response
        active += 1
        peak = max(peak, active)
        try:
            entered.put_nowait(body["params"]["arguments"]["q"])
            await release.wait()
        finally:
            active -= 1
        return _data(body, {"items": []})

    client = make_client(handler)
    async with asyncio.timeout(1):
        tasks = [asyncio.create_task(client.call("search", {"q": i})) for i in range(5)]
        try:
            assert {await entered.get() for _ in range(4)} == {0, 1, 2, 3}
            assert entered.empty()
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert await entered.get() == 4
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert active == 0
    assert peak == 4
    assert initialize_calls == 1


@pytest.mark.parametrize("media_type", ["application/json", "text/event-stream"])
async def test_stops_reading_oversized_responses(
    make_client, monkeypatch, media_type
) -> None:
    monkeypatch.setattr(mcp, "_MAX_RESPONSE_BYTES", 4096)

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"x" * 3000
            yield b"x" * 3000
            raise AssertionError("read beyond the maximum response size")

        async def aclose(self) -> None:
            self.closed = True

    stream = Stream()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return _initialize(body) or httpx.Response(
            200, headers={"Content-Type": media_type}, stream=stream
        )

    client = make_client(handler)
    with pytest.raises(McpError) as caught:
        await client.call("search", {})
    assert caught.value.unavailable
    assert not caught.value.retryable
    assert stream.closed


async def test_session_expiry_reinitializes_before_retry(make_client) -> None:
    initialized = 0
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialized
        body = json.loads(request.content)
        session = request.headers.get("Mcp-Session-Id")
        seen.append((body["method"], session))
        match body["method"]:
            case "initialize":
                initialized += 1
                return _rpc(
                    body,
                    _capture("init")["result"],
                    headers={"Mcp-Session-Id": f"session-{initialized}"},
                )
            case "notifications/initialized":
                return httpx.Response(200)
            case "tools/call":
                if session == "session-1":
                    return httpx.Response(404)
                return _data(body, {"items": []})
        raise AssertionError(body)

    client = make_client(handler)
    assert await client.call("search", {}) == {"items": []}
    assert seen == [
        ("initialize", None),
        ("notifications/initialized", "session-1"),
        ("tools/call", "session-1"),
        ("initialize", None),
        ("notifications/initialized", "session-2"),
        ("tools/call", "session-2"),
    ]


async def test_late_link_rejection_keeps_reinitialized_session(make_client) -> None:
    waiting = asyncio.Event()
    release = asyncio.Event()
    initialized = 0
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialized
        body = json.loads(request.content)
        session = request.headers.get("Mcp-Session-Id")
        match body["method"]:
            case "initialize":
                initialized += 1
                return _rpc(
                    body,
                    _capture("init")["result"],
                    headers={"Mcp-Session-Id": f"session-{initialized}"},
                )
            case "notifications/initialized":
                return httpx.Response(202)
            case "tools/call":
                product_id = body["params"]["arguments"]["products"][0]["id"]
                calls.append((product_id, session))
                if session == "session-1":
                    if product_id == 1:
                        waiting.set()
                        await release.wait()
                    return httpx.Response(404)
                return _data(body, {"link": f"https://vkusvill.ru/cart/{product_id}"})
        raise AssertionError(body)

    client = make_client(handler)

    async def create_link(product_id: int) -> dict:
        return await client.call(
            CART_LINK_TOOL, {"products": [{"id": product_id, "q": 1}]}
        )

    async def freshen_session() -> dict:
        await waiting.wait()
        try:
            return await create_link(2)
        finally:
            release.set()

    first, second = await asyncio.wait_for(
        asyncio.gather(create_link(1), freshen_session(), return_exceptions=True), 1
    )

    assert first == {"link": "https://vkusvill.ru/cart/1"}
    assert second == {"link": "https://vkusvill.ru/cart/2"}
    assert initialized == 2
    assert calls == [
        (1, "session-1"),
        (2, "session-1"),
        (2, "session-2"),
        (1, "session-2"),
    ]


async def test_link_does_not_retry_session_rejection_during_handshake(
    make_client,
) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["method"])
        if body["method"] == "initialize":
            return _rpc(
                body,
                _capture("init")["result"],
                headers={"Mcp-Session-Id": "session-1"},
            )
        assert request.headers["Mcp-Session-Id"] == "session-1"
        return httpx.Response(404)

    client = make_client(handler)
    with pytest.raises(McpError):
        await client.call(CART_LINK_TOOL, {})
    assert seen == ["initialize", "notifications/initialized"]


async def test_outer_deadline_closes_sse_and_releases_slot(make_client) -> None:
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            started.set()
            yield b": heartbeat\n\n"
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    started = asyncio.Event()
    stream = Stream()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if (response := _initialize(body)) is not None:
            return response
        query = body["params"]["arguments"]["q"]
        seen.append(query)
        if query == "slow":
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=stream
            )
        return _data(body, {"items": []})

    client = make_client(handler, concurrency=1)
    async with asyncio.timeout(1):
        task = asyncio.create_task(client.call("search", {"q": "slow"}))
        await started.wait()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                await client.call("search", {"q": "queued"})
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        assert await client.call("search", {"q": "fresh"}) == {"items": []}
    assert seen == ["slow", "fresh"]

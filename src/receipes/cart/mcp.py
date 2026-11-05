import asyncio
import json
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from decimal import Decimal, DecimalException
from itertools import count
from time import monotonic

import httpx

_PROTOCOL_VERSION = "2025-03-26"
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_MAX_RESPONSE_BYTES = 256 * 1024
_CACHE_LIMIT = 128
_SSE_NEWLINE = re.compile(rb"\r\n?|\n")


class McpError(Exception):
    def __init__(
        self,
        message: str,
        *,
        unavailable: bool = False,
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.unavailable = unavailable
        self.retryable = retryable
        self.http_status = http_status


def _invalid_response() -> McpError:
    return McpError("Invalid MCP response", unavailable=True)


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def _json_object(raw: str | bytes | bytearray) -> dict:
    try:
        value = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, DecimalException, RecursionError):
        raise _invalid_response() from None
    if not isinstance(value, dict):
        raise _invalid_response()
    return value


async def _bounded_bytes(response: httpx.Response) -> AsyncIterator[bytes]:
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > _MAX_RESPONSE_BYTES:
            raise McpError("MCP response is too large", unavailable=True)
        yield chunk


async def _sse_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    line = bytearray()
    data = []
    skip_lf = False
    first_line = True
    async for chunk in chunks:
        if skip_lf:
            chunk = chunk.removeprefix(b"\n")
        skip_lf = chunk.endswith(b"\r")
        start = 0
        for newline in _SSE_NEWLINE.finditer(chunk):
            line.extend(chunk[start : newline.start()])
            start = newline.end()
            value = bytes(line)
            line.clear()
            if first_line:
                value = value.removeprefix(b"\xef\xbb\xbf")
                first_line = False
            if not value and data:
                yield b"\n".join(data)
                data.clear()
            field, _, field_value = value.partition(b":")
            if field == b"data":
                data.append(field_value.removeprefix(b" "))
        line.extend(chunk[start:])


def _has_id(message: dict, request_id: int) -> bool:
    value = message.get("id")
    return type(value) is int and value == request_id


async def _read_message(response: httpx.Response, request_id: int) -> dict:
    media_type = (
        response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    )
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise _invalid_response()
    chunks = _bounded_bytes(response)
    if media_type == "application/json":
        raw = bytearray()
        async for chunk in chunks:
            raw.extend(chunk)
        message = _json_object(raw)
        if _has_id(message, request_id):
            return message
    elif media_type == "text/event-stream":
        async for event in _sse_events(chunks):
            message = _json_object(event)
            if _has_id(message, request_id):
                return message
        raise McpError(
            "MCP stream ended without a response", unavailable=True, retryable=True
        )
    raise _invalid_response()


def _tool_error(error: object, *, unavailable: bool = False) -> McpError:
    error = error if isinstance(error, dict) else {}
    status = error.get("http_status")
    if type(status) is not int or not 400 <= status <= 599:
        status = None
    retryable = error.get("retryable")
    if type(retryable) is not bool:
        retryable = status in _RETRYABLE_STATUS
    unavailable = (
        unavailable
        or retryable
        or (status is not None and (status in (401, 403, 408, 429) or status >= 500))
    )
    return McpError(
        "MCP tool failed",
        unavailable=unavailable,
        retryable=retryable,
        http_status=status,
    )


def _rpc_result(message: dict) -> dict:
    if message.get("jsonrpc") != "2.0" or ("result" in message) == ("error" in message):
        raise _invalid_response()
    if "error" in message:
        error = message["error"]
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise _invalid_response()
        details = error.get("data")
        if isinstance(details, dict):
            details = details.get("error", details)
        raise _tool_error(
            details if isinstance(details, dict) else error,
            unavailable=error.get("code") in (-32700, -32600, -32601, -32603),
        )
    result = message["result"]
    if not isinstance(result, dict):
        raise _invalid_response()
    return result


def _tool_data(result: dict) -> tuple[dict, str]:
    failed = result.get("isError", False)
    content = result.get("content")
    if type(failed) is not bool or not isinstance(content, list):
        raise _invalid_response()
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = _json_object(text)
        except McpError:
            continue
        if payload.get("ok") is False:
            raise _tool_error(payload.get("error", result.get("error")))
        if (
            not failed
            and payload.get("ok") is True
            and isinstance(payload.get("data"), dict)
        ):
            return payload["data"], text
    if failed:
        raise _tool_error(result.get("error"))
    raise _invalid_response()


class McpClient:
    def __init__(
        self,
        url: str,
        *,
        timeout: float = 5,
        retries: int = 1,
        concurrency: int = 4,
        cache_ttl: float = 900,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._retries = retries
        self._cache_ttl = cache_ttl
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json, text/event-stream",
                "Accept-Encoding": "identity",
            },
        )
        self._slots = asyncio.Semaphore(concurrency)
        self._init_lock = asyncio.Lock()
        self._session_headers: dict[str, str] | None = None
        self._ids = count(1)
        self._cache: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()

    async def aclose(self) -> None:
        await self._http.aclose()
        self._cache.clear()

    async def call(self, tool: str, arguments: dict) -> dict:
        key = (
            tool,
            json.dumps(
                arguments, sort_keys=True, separators=(",", ":"), allow_nan=False
            ),
        )
        now = monotonic()
        while self._cache and next(iter(self._cache.values()))[0] <= now:
            self._cache.popitem(last=False)
        if (entry := self._cache.get(key)) is not None:
            return _json_object(entry[1])["data"]

        params = {"name": tool, "arguments": arguments}
        retries = self._retries
        while True:
            try:
                await self._ensure_session()
                async with self._slots:
                    if (headers := self._session_headers) is None:
                        # Another call observed session expiry while this call queued.
                        continue
                    result, _ = await self._post("tools/call", params, headers=headers)
                data, text = _tool_data(result)
            except McpError as exc:
                if not exc.retryable or retries <= 0:
                    raise
                retries -= 1
            else:
                self._cache[key] = (monotonic() + self._cache_ttl, text)
                self._cache.move_to_end(key)
                if len(self._cache) > _CACHE_LIMIT:
                    self._cache.popitem(last=False)
                return data

    async def _ensure_session(self) -> None:
        if self._session_headers is not None:
            return
        async with self._init_lock:
            if self._session_headers is None:
                async with self._slots:
                    self._session_headers = await self._initialize()

    async def _initialize(self) -> dict[str, str]:
        try:
            result, session_id = await self._post(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "smartmeal-recipes", "version": "0.1.0"},
                },
            )
            if result.get("protocolVersion") != _PROTOCOL_VERSION:
                raise _invalid_response()
            headers = {"MCP-Protocol-Version": _PROTOCOL_VERSION}
            if session_id is not None:
                if re.fullmatch(r"[!-~]+", session_id) is None:
                    raise _invalid_response()
                headers["Mcp-Session-Id"] = session_id
            await self._post("notifications/initialized", headers=headers)
        except McpError as exc:
            exc.unavailable = True
            raise
        return headers

    async def _post(
        self,
        method: str,
        params: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict, str | None]:
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        request_id = None if method == "notifications/initialized" else next(self._ids)
        if request_id is not None:
            body["id"] = request_id
        try:
            async with (
                asyncio.timeout(self._timeout),
                self._http.stream(
                    "POST", self._url, json=body, headers=headers
                ) as response,
            ):
                status = response.status_code
                if not response.is_success:
                    expired = (
                        status == 404
                        and headers is not None
                        and "Mcp-Session-Id" in headers
                    )
                    if expired and self._session_headers is headers:
                        self._session_headers = None
                    raise McpError(
                        "MCP HTTP request failed",
                        unavailable=True,
                        retryable=expired or status in _RETRYABLE_STATUS,
                        http_status=status,
                    )
                if request_id is None:
                    if status not in (200, 202):
                        raise _invalid_response()
                    return {}, None
                message = await _read_message(response, request_id)
                return _rpc_result(message), response.headers.get("Mcp-Session-Id")
        except (TimeoutError, httpx.TimeoutException):
            raise McpError("MCP timed out", unavailable=True, retryable=True) from None
        except httpx.RequestError as exc:
            raise McpError(
                "MCP transport failed",
                unavailable=True,
                retryable=isinstance(
                    exc,
                    httpx.NetworkError | httpx.RemoteProtocolError | httpx.ProxyError,
                ),
            ) from None

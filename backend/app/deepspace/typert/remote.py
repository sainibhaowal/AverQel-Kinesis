"""Authenticated Python client for the Typert HTTP and WebSocket transports."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect

from app.deepspace.typert.errors import RemoteError

_MAX_FRAME_BYTES = 1_000_000


def _remote_error(payload: Any) -> RemoteError:
    details = payload.get("detail", payload) if isinstance(payload, Mapping) else payload
    if not isinstance(details, Mapping):
        return RemoteError("gateway/internal", "The remote capability returned an invalid error.")
    code = details.get("code")
    message = details.get("message")
    extra = details.get("details")
    try:
        return RemoteError(
            str(code),
            str(message) if message is not None else None,
            details=extra if isinstance(extra, Mapping) else None,
        )
    except ValueError:
        return RemoteError("gateway/internal", "The remote capability returned an unknown error.")


class TypertRemoteClient:
    """Call one DeepSpace Typert API with a bearer token.

    ``base_url`` is the API root, including any deployment prefix such as
    ``/api/v1``. Tokens are sent in the Authorization header, never in a URL.
    """

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTP(S) API root without credentials or query.")
        token = access_token.strip()
        if not token:
            raise ValueError("An access token is required.")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number.")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None

    async def invoke(self, endpoint: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """Invoke one unary endpoint and return its decoded result."""
        response = await self._http.post(
            f"{self._base_url}/deepspace/typert/invoke",
            headers=self._headers,
            json={"endpoint": endpoint, "arguments": dict(arguments or {})},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RemoteError(
                "gateway/internal", "The remote capability returned invalid JSON."
            ) from exc
        if response.is_error:
            raise _remote_error(payload)
        if not isinstance(payload, Mapping) or "result" not in payload:
            raise RemoteError("gateway/internal", "The remote capability response is malformed.")
        return payload["result"]

    async def open_stream(
        self,
        endpoint: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> RemoteTypertStream:
        """Open one authenticated bidirectional capability stream."""
        parsed = urlsplit(self._base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = f"{parsed.path.rstrip('/')}/deepspace/typert/invoke/stream"
        uri = urlunsplit((scheme, parsed.netloc, path, "", ""))
        connection = await connect(
            uri,
            additional_headers=self._headers,
            open_timeout=self._timeout,
        )
        try:
            first_frame = _encode_frame({"endpoint": endpoint, "arguments": dict(arguments or {})})
            await connection.send(first_frame)
            first = _decode_frame(await asyncio.wait_for(connection.recv(), timeout=self._timeout))
            if first.get("event") == "error":
                raise _remote_error(first)
            if first.get("event") != "ready":
                raise RemoteError("gateway/internal", "The stream handshake was malformed.")
            return RemoteTypertStream(connection)
        except BaseException:
            await connection.close()
            raise

    async def close(self) -> None:
        """Close the owned HTTP client, if this client created it."""
        if self._owns_http:
            await self._http.aclose()
            self._owns_http = False

    async def __aenter__(self) -> TypertRemoteClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


class RemoteTypertStream:
    """One open remote stream with uplink send and downlink iteration."""

    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection
        self._ended = False
        self._closed = False

    async def send(self, value: Any) -> None:
        """Send one JSON uplink value."""
        if self._ended or self._closed:
            raise RemoteError("stream/closed", "The remote stream uplink has ended.")
        await self._connection.send(_encode_frame({"event": "uplink", "value": value}))

    async def end(self) -> None:
        """Finish the uplink while allowing the downlink to drain."""
        if self._ended or self._closed:
            return
        self._ended = True
        await self._connection.send('{"event":"end"}')

    async def cancel(self) -> None:
        """Cancel both directions and close the WebSocket."""
        if self._closed:
            return
        self._ended = True
        try:
            await self._connection.send('{"event":"cancel"}')
        finally:
            await self.close()

    async def receive(self) -> Any:
        """Read one downlink value; raise ``stream/closed`` on completion."""
        if self._closed:
            raise RemoteError("stream/closed", "The remote stream is closed.")
        try:
            frame = _decode_frame(await self._connection.recv())
        except Exception as exc:
            if isinstance(exc, RemoteError):
                raise
            raise RemoteError("gateway/unavailable", "The remote stream disconnected.") from exc
        event_name = frame.get("event")
        if event_name == "downlink":
            return frame.get("value")
        if event_name == "error":
            raise _remote_error(frame)
        if event_name == "complete":
            await self.end()
            raise RemoteError("stream/closed", "The remote stream has completed.")
        raise RemoteError("gateway/internal", "The remote stream returned an unknown frame.")

    async def __aiter__(self) -> AsyncIterator[Any]:
        while True:
            try:
                yield await self.receive()
            except RemoteError as exc:
                if exc.code == "stream/closed":
                    return
                raise

    async def close(self) -> None:
        """Close this stream idempotently."""
        if self._closed:
            return
        self._closed = True
        await self._connection.close()

    async def __aenter__(self) -> RemoteTypertStream:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if not self._ended:
            await self.cancel()
        else:
            await self.close()


def _decode_frame(raw: str | bytes) -> dict[str, Any]:
    """Decode and validate one remote JSON frame."""
    raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if len(raw_bytes) > _MAX_FRAME_BYTES:
        raise RemoteError("gateway/bad-request", "The remote frame exceeds the 1 MB limit.")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RemoteError("gateway/internal", "The remote stream returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise RemoteError("gateway/internal", "The remote stream frame is not an object.")
    return value


def _encode_frame(frame: Mapping[str, Any]) -> str:
    """Encode one bounded JSON frame for a remote stream."""
    try:
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RemoteError(
            "gateway/bad-request", "The stream value is not JSON-compatible."
        ) from exc
    if len(encoded.encode("utf-8")) > _MAX_FRAME_BYTES:
        raise RemoteError("gateway/bad-request", "The stream frame exceeds the 1 MB limit.")
    return encoded


__all__ = ["RemoteTypertStream", "TypertRemoteClient"]

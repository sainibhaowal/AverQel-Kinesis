"""A small typed client for the DeepSpace HTTP API.

The DeepSpace command line is a client of the same public HTTP API the web
application uses, so every call in this module targets a route the server
already serves and every method returns the JSON that route produced.

The routes this module uses are:

* ``GET  /api/v1/deepspace/chats`` - the conversations of the caller.
* ``GET  /api/v1/deepspace/chats/{id}/messages`` - the messages of one
  conversation.
* ``POST /api/v1/deepspace/chats/stream`` - one message, answered as a
  server-sent event stream.
* ``GET  /api/v1/deepspace/sandbox/backend`` - the sandbox the server would
  use for shell tools.
* ``GET  /api/v1/deepspace/chats/plugins`` - the plugins the deployment
  offers.
* ``GET  /api/v1/deepspace/chats/presets`` - the agent presets a conversation
  may run in.
* ``GET  /api/v1/deepspace/chats/{id}/terminals`` - the live terminal
  sessions of one conversation.
* ``GET  /api/v1/deepspace/chats/{id}/plan`` - the plan of one conversation.

Only the standard library is used, so the client adds no dependency to the
deployment that serves the API.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

__all__ = ["DeepSpaceApiClient", "DeepSpaceApiError"]


#: The base URL the client uses when the environment names none.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: The prefix every DeepSpace API route sits under.
API_PREFIX = "/api/v1"

#: The seconds one request may wait before it is abandoned.  A message runs
#: through a provider and may call tools, so the wait covers a whole turn.
DEFAULT_TIMEOUT_SECONDS = 300.0

#: The environment variable that names the API base URL.
BASE_URL_ENV_VAR = "AKS_API_URL"

#: The environment variable that holds the bearer token.
TOKEN_ENV_VAR = "AKS_API_TOKEN"

#: The route that lists the conversations.
CHATS_ROUTE = "/deepspace/chats"

#: The route that answers one message as a server-sent event stream.
STREAM_ROUTE = "/deepspace/chats/stream"

#: The route that reports the sandbox the server would use.
SANDBOX_ROUTE = "/deepspace/sandbox/backend"


class DeepSpaceApiError(Exception):
    """A DeepSpace API call that did not produce a usable answer.

    Args:
        message: The message the caller should show.
        status: The HTTP status the server returned.  It is absent when the
            request never reached the server.
        code: The server's error code, when it sent one.

    Attributes:
        message: The message the caller should show.
        status: The HTTP status the server returned.
        code: The server's error code.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def __str__(self) -> str:
        """Return the message, prefixed with the status when there is one."""
        if self.status is None:
            return self.message
        return f"HTTP {self.status}: {self.message}"


class DeepSpaceApiClient:
    """A client for the DeepSpace HTTP API.

    Args:
        base_url: The API base URL.  It defaults to the ``AKS_API_URL``
            environment variable and then to ``http://127.0.0.1:8000``.
        token: The bearer token sent with every request.  It defaults to the
            ``AKS_API_TOKEN`` environment variable.
        timeout: The seconds one request may wait.

    Attributes:
        base_url: The API base URL, without a trailing slash.
        token: The bearer token, empty when the deployment accepts a request
            without one.
        timeout: The seconds one request may wait.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        resolved_base = base_url if base_url is not None else os.environ.get(BASE_URL_ENV_VAR, "")
        self.base_url = (resolved_base.strip() or DEFAULT_BASE_URL).rstrip("/")
        resolved_token = token if token is not None else os.environ.get(TOKEN_ENV_VAR, "")
        self.token = resolved_token.strip()
        self.timeout = timeout

    def list_chats(self) -> dict[str, Any]:
        """List the conversations the caller owns.

        Returns:
            The conversation list response.
        """
        return self._request("GET", CHATS_ROUTE)

    def get_chat(self, conversation_id: str) -> dict[str, Any]:
        """Read one conversation's messages.

        Args:
            conversation_id: The conversation to read.

        Returns:
            The chat history response.
        """
        route = f"{CHATS_ROUTE}/{_segment(conversation_id)}/messages"
        return self._request("GET", route)

    def send_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        """Send one message to a conversation and read the answer.

        The server answers a message with a server-sent event stream, so this
        method reads that stream and returns the finished answer together with
        the identifiers of the run that produced it.

        Args:
            conversation_id: The conversation that receives the message.
            text: The message to send.

        Returns:
            The answer, and the identifiers of the run that produced it.
        """
        payload = {
            "message": text,
            "conversation_id": conversation_id,
            "client_request_id": str(uuid.uuid4()),
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._url(STREAM_ROUTE),
            data=body,
            method="POST",
            headers=self._headers(accept="text/event-stream", body=body),
        )
        raw = self._send(request)
        return _answer_from_stream(
            raw.decode("utf-8", errors="replace"),
            conversation_id=conversation_id,
        )

    def get_sandbox_status(self) -> dict[str, Any]:
        """Report the sandbox the server would use for shell tools.

        Returns:
            The sandbox status response.
        """
        return self._request("GET", SANDBOX_ROUTE)

    def list_plugins(self) -> dict[str, Any]:
        """List the plugins the deployment offers.

        Returns:
            The plugin catalogue response.
        """
        return self._request("GET", f"{CHATS_ROUTE}/plugins")

    def list_presets(self) -> dict[str, Any]:
        """List the agent presets a conversation may run in.

        Returns:
            The preset list response.
        """
        return self._request("GET", f"{CHATS_ROUTE}/presets")

    def list_terminals(self, conversation_id: str) -> dict[str, Any]:
        """List one conversation's live terminal sessions.

        Args:
            conversation_id: The conversation whose sessions to list.

        Returns:
            The terminal session list response.
        """
        route = f"{CHATS_ROUTE}/{_segment(conversation_id)}/terminals"
        return self._request("GET", route)

    def list_workspaces(self) -> dict[str, Any]:
        """List the workspaces the signed-in account owns.

        Returns:
            The workspace catalogue response.
        """
        return self._request("GET", f"{CHATS_ROUTE}/workspaces")

    def list_plans(self, conversation_id: str) -> dict[str, Any]:
        """Read one conversation's plan.

        Args:
            conversation_id: The conversation whose plan to read.

        Returns:
            The plan response.
        """
        route = f"{CHATS_ROUTE}/{_segment(conversation_id)}/plan"
        return self._request("GET", route)

    def _url(self, route: str) -> str:
        """Return the absolute URL of one API route.

        Args:
            route: The route under the API prefix.

        Returns:
            The absolute URL.
        """
        return f"{self.base_url}{API_PREFIX}{route}"

    def _headers(
        self, *, accept: str = "application/json", body: bytes | None = None
    ) -> dict[str, str]:
        """Return the headers one request carries.

        Args:
            accept: The media type the caller accepts.
            body: The request body, when the request carries one.

        Returns:
            The request headers.
        """
        headers = {"Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self, method: str, route: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one JSON request and return the parsed response.

        Args:
            method: The HTTP method.
            route: The route under the API prefix.
            payload: The request body, when the request carries one.

        Returns:
            The parsed response.
        """
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self._url(route),
            data=body,
            method=method,
            headers=self._headers(body=body),
        )
        return _decode_json(self._send(request))

    def _send(self, request: urllib.request.Request) -> bytes:
        """Perform one HTTP request and return the response body.

        Args:
            request: The request to send.

        Returns:
            The response body.

        Raises:
            DeepSpaceApiError: When the server refuses the request or the
                request never reaches the server.
        """
        try:
            response: Any = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            raise DeepSpaceApiError(
                _error_message(payload, fallback=str(exc.reason)),
                status=exc.code,
                code=_error_code(payload),
            ) from exc
        except urllib.error.URLError as exc:
            raise DeepSpaceApiError(
                f"The DeepSpace API at {self.base_url} could not be reached: {exc.reason}"
            ) from exc
        except OSError as exc:
            raise DeepSpaceApiError(
                f"The DeepSpace API at {self.base_url} could not be reached: {exc}"
            ) from exc
        try:
            raw: bytes = response.read()
        except OSError as exc:
            raise DeepSpaceApiError(
                f"The DeepSpace API at {self.base_url} closed the connection: {exc}"
            ) from exc
        finally:
            response.close()
        return raw


def _segment(value: str) -> str:
    """Return one path segment, escaped for use in a URL.

    Args:
        value: The raw segment.

    Returns:
        The escaped segment.
    """
    return urllib.parse.quote(str(value), safe="")


def _decode_json(raw: bytes) -> dict[str, Any]:
    """Return the parsed body of a JSON response.

    Args:
        raw: The response body.

    Returns:
        The parsed response.  A response that is not a JSON object is wrapped
        in a ``value`` field.

    Raises:
        DeepSpaceApiError: When the body is not JSON.
    """
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeepSpaceApiError("The DeepSpace API returned a response that is not JSON.") from exc
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def _error_message(payload: bytes, *, fallback: str) -> str:
    """Return the message a failed response carries.

    Args:
        payload: The response body.
        fallback: The message to use when the body carries none.

    Returns:
        The server's message.
    """
    parsed = _error_payload(payload)
    if parsed is None:
        text = payload.decode("utf-8", errors="replace").strip()
        return text or fallback
    error = parsed.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    for key in ("detail", "message"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _error_code(payload: bytes) -> str | None:
    """Return the error code a failed response carries.

    Args:
        payload: The response body.

    Returns:
        The server's error code, or ``None`` when it sent none.
    """
    parsed = _error_payload(payload)
    if parsed is None:
        return None
    error = parsed.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return None


def _error_payload(payload: bytes) -> dict[str, Any] | None:
    """Return the parsed body of a failed response, when it is an object.

    Args:
        payload: The response body.

    Returns:
        The parsed body, or ``None`` when the body is not a JSON object.
    """
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_sse_frames(payload: str) -> list[tuple[str, dict[str, Any]]]:
    """Return the server-sent events a stream body carries.

    Args:
        payload: The stream body.

    Returns:
        The event name and data of every frame, in the order they arrived.
    """
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in payload.replace("\r\n", "\n").split("\n\n"):
        stripped = block.strip()
        if not stripped:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in stripped.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw": raw}
        frames.append((event, data if isinstance(data, dict) else {"value": data}))
    return frames


def _answer_from_stream(payload: str, *, conversation_id: str) -> dict[str, Any]:
    """Return the answer a message stream produced.

    Args:
        payload: The stream body.
        conversation_id: The conversation the message was sent to.

    Returns:
        The answer, and the identifiers of the run that produced it.

    Raises:
        DeepSpaceApiError: When the stream reported a failure.
    """
    content = ""
    answer: dict[str, Any] = {
        "conversation_id": conversation_id,
        "message_id": None,
        "run_id": None,
        "status": None,
        "content": content,
    }
    for event, data in _parse_sse_frames(payload):
        if event == "delta":
            text = data.get("text")
            if isinstance(text, str):
                content += text
        elif event == "replace":
            replacement = data.get("content")
            if isinstance(replacement, str):
                content = replacement
        elif event == "done":
            answer["message_id"] = _optional_text(data.get("message_id"))
            answer["run_id"] = _optional_text(data.get("run_id"))
            answer["status"] = _optional_text(data.get("status"))
            answer["conversation_id"] = (
                _optional_text(data.get("conversation_id")) or conversation_id
            )
        elif event == "error":
            raise DeepSpaceApiError(
                _optional_text(data.get("message")) or "The DeepSpace run failed.",
                code=_optional_text(data.get("code")),
            )
    answer["content"] = content
    return answer


def _optional_text(value: Any) -> str | None:
    """Return a value as text, or ``None`` when it is absent.

    Args:
        value: The raw value.

    Returns:
        The text, or ``None``.
    """
    if value is None:
        return None
    text = str(value)
    return text or None

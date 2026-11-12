"""Language Server Protocol (LSP) service for DeepSpace.

Ported from NeoSIS @averqel/neosis-lsp and @averqel/neosis-lsp-stdio.
Provides language server registry and query routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.core.config import Settings
from app.deepspace.services.subprocess_stdio import (
    SubprocessCollect,
    SubprocessHandle,
    SubprocessStdio,
    spawn_with_stdio,
)

logger = logging.getLogger(__name__)


# LSP Operations
LspOperation = Literal["goToDefinition", "findReferences", "goToImplementation", "hover"]


@dataclass(frozen=True, slots=True)
class LspPosition:
    """Zero-based UTF-16 position (matching LSP spec)."""

    line: int
    character: int


@dataclass(frozen=True, slots=True)
class LspLocation:
    """LSP location result."""

    uri: str
    range: dict[str, Any]  # {start: {line, character}, end: {line, character}}


@dataclass(frozen=True, slots=True)
class LspHover:
    """LSP hover result."""

    contents: str
    range: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LspQueryResult:
    """Result of an LSP query."""

    operation: LspOperation
    locations: list[LspLocation] = field(default_factory=list)
    hover: LspHover | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LspProviderConfig:
    """Configuration for an LSP provider."""

    id: str
    command: list[str]
    extension_to_language: dict[str, str]  # ".ts" -> "typescript"
    env: dict[str, str] = field(default_factory=dict)
    initialization_options: dict[str, Any] | None = None
    configuration: dict[str, Any] | None = None
    max_message_bytes: int = 16_000_000
    max_stderr_bytes: int = 1_000_000
    max_document_bytes: int = 4_000_000
    shutdown_timeout_ms: int = 5_000
    kill_grace_ms: int = 2_000


@dataclass(frozen=True, slots=True)
class Route:
    """Route mapping extension to provider."""

    provider: LspProvider
    language_id: str


class LspError(Exception):
    """LSP-related error with code."""

    def __init__(self, message: str, code: str = "LSP_ERROR"):
        super().__init__(message)
        self.code = code


def normalize_extension(raw_ext: str) -> str:
    """Normalize extension to lowercase leading-dot form."""
    ext = raw_ext.strip().lower()
    if not ext.startswith("."):
        ext = "." + ext
    return ext


def final_extension(file_path: str) -> str:
    """Extract final extension from file path."""
    # Split on both / and \
    last_slash = max(file_path.rfind("/"), file_path.rfind("\\"))
    base = file_path[last_slash + 1 :] if last_slash >= 0 else file_path
    dot = base.rfind(".")
    if dot <= 0:  # no extension or dotfile
        return ""
    return base[dot:].lower()


class LspConnection:
    """Manages a single LSP server connection."""

    def __init__(self, config: LspProviderConfig, workspace_root: Path):
        self.config = config
        self.workspace_root = workspace_root
        self.process: asyncio.subprocess.Process | None = None
        self._process_handle: SubprocessHandle | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._initialized = False
        self._capabilities: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the LSP server process."""
        self._process_handle = await spawn_with_stdio(
            list(self.config.command),
            cwd=str(self.workspace_root),
            env=dict(self.config.env),
            stdio=SubprocessStdio(
                stdin="pipe",
                stdout="pipe",
                stderr=SubprocessCollect(max_bytes=65_536),
            ),
            grace_ms=self.config.shutdown_timeout_ms,
        )
        self.process = self._process_handle.process
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await self._initialize()
        except BaseException:
            await self._process_handle.terminate_and_wait()
            raise

    async def _initialize(self) -> None:
        """Send initialize request."""
        init_params = {
            "processId": os.getpid(),
            "rootUri": self.workspace_root.as_uri(),
            "capabilities": {},
        }
        if self.config.initialization_options:
            init_params["initializationOptions"] = self.config.initialization_options

        response = await self._send_request("initialize", init_params)
        self._capabilities = response.get("capabilities", {})

        # Send initialized notification
        await self._send_notification("initialized", {})

        self._initialized = True

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request and wait for response."""
        if not self.process or not self.process.stdin:
            raise LspError("LSP server not running", "LSP_NOT_RUNNING")

        self._request_id += 1
        req_id = self._request_id

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        message = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        await self._write_message(message)

        try:
            response = await asyncio.wait_for(future, timeout=30.0)
            return response
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise LspError(f"LSP request {method} timed out", "LSP_TIMEOUT") from None

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification (no response expected)."""
        if not self.process or not self.process.stdin:
            return

        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._write_message(message)

    async def _write_message(self, message: dict[str, Any]) -> None:
        """Write a JSON-RPC message to the process stdin."""
        assert self.process is not None and self.process.stdin is not None
        content = json.dumps(message)
        content_bytes = content.encode("utf-8")
        header = f"Content-Length: {len(content_bytes)}\r\n\r\n"
        self.process.stdin.write(header.encode("ascii") + content_bytes)
        await self.process.stdin.drain()

    async def _read_loop(self) -> None:
        """Read responses from the LSP server stdout."""
        if not self.process or not self.process.stdout:
            return

        while self.process.returncode is None:
            try:
                header = await self.process.stdout.readuntil(b"\r\n\r\n")
                content_length = None
                for header_line in header[:-4].decode("ascii").split("\r\n"):
                    if header_line.lower().startswith("content-length:"):
                        content_length = int(header_line.split(":", 1)[1].strip())
                        break
                if content_length is None or not 0 <= content_length <= 4_000_000:
                    raise LspError("The language server sent an invalid frame length", "LSP_FRAME")
                content = await self.process.stdout.readexactly(content_length)
                response = json.loads(content.decode("utf-8"))
                if isinstance(response, dict):
                    self._handle_response(response)
            except asyncio.IncompleteReadError:
                break
            except Exception as e:
                logger.error("Error in LSP read loop: %s", e)
                break

    def _handle_response(self, response: dict[str, Any]) -> None:
        """Handle a JSON-RPC response."""
        req_id = response.get("id")
        if req_id is not None and req_id in self._pending:
            future = self._pending.pop(req_id)
            if "error" in response:
                future.set_exception(
                    LspError(response["error"].get("message", "Unknown error"), "LSP_ERROR")
                )
            else:
                future.set_result(response.get("result", {}))

    async def query(
        self,
        operation: LspOperation,
        file_path: str,
        position: LspPosition,
    ) -> LspQueryResult:
        """Execute an LSP query."""
        if not self._initialized:
            raise LspError("LSP server not initialized", "LSP_NOT_INITIALIZED")

        # Check if operation is supported
        if not self._supports_operation(operation):
            return LspQueryResult(
                operation=operation,
                error=f"Operation {operation} not supported by this server",
            )

        # Convert file path to URI
        uri = Path(file_path).resolve().as_uri()

        # Convert position to LSP format (zero-based)
        lsp_position = {"line": position.line - 1, "character": position.character - 1}

        try:
            if operation == "hover":
                result = await self._send_request(
                    "textDocument/hover",
                    {
                        "textDocument": {"uri": uri},
                        "position": lsp_position,
                    },
                )
                hover = None
                if result:
                    contents = result.get("contents", "")
                    if isinstance(contents, list):
                        contents = "\n".join(
                            c.get("value", "") if isinstance(c, dict) else str(c) for c in contents
                        )
                    elif isinstance(contents, dict):
                        contents = contents.get("value", "")
                    hover = LspHover(contents=contents, range=result.get("range"))
                return LspQueryResult(operation=operation, hover=hover)

            elif operation in ("goToDefinition", "goToImplementation", "findReferences"):
                method = {
                    "goToDefinition": "textDocument/definition",
                    "goToImplementation": "textDocument/implementation",
                    "findReferences": "textDocument/references",
                }[operation]

                params = {
                    "textDocument": {"uri": uri},
                    "position": lsp_position,
                }
                if operation == "findReferences":
                    params["context"] = {"includeDeclaration": True}

                result = await self._send_request(method, params)
                locations = []
                for loc in result or []:
                    locations.append(
                        LspLocation(
                            uri=loc.get("uri", ""),
                            range=loc.get("range", {}),
                        )
                    )
                return LspQueryResult(operation=operation, locations=locations)

        except LspError:
            raise
        except Exception as e:
            logger.error("LSP query failed: %s", e)
            return LspQueryResult(operation=operation, error=str(e))

        return LspQueryResult(operation=operation, error="Unknown operation")

    def _supports_operation(self, operation: LspOperation) -> bool:
        """Check if the server supports an operation."""
        caps = self._capabilities
        if operation == "hover":
            return caps.get("hoverProvider", False)
        elif operation == "goToDefinition":
            return caps.get("definitionProvider", False)
        elif operation == "goToImplementation":
            return caps.get("implementationProvider", False)
        elif operation == "findReferences":
            return caps.get("referencesProvider", False)
        return False

    async def shutdown(self) -> None:
        """Shutdown the LSP server."""
        if self._initialized:
            try:
                await self._send_request("shutdown", {})
                await self._send_notification("exit", {})
            except Exception:
                pass

        if self.process and self._process_handle:
            try:
                await asyncio.wait_for(
                    self._process_handle.wait(),
                    timeout=self.config.shutdown_timeout_ms / 1000,
                )
            except TimeoutError:
                await self._process_handle.terminate_and_wait()
            except Exception:
                pass

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass


class LspProvider:
    """Manages LSP connections for a provider across workspaces."""

    def __init__(self, config: LspProviderConfig):
        self.config = config
        self._connections: dict[str, LspConnection] = {}

    def _workspace_key(self, workspace_root: Path) -> str:
        return str(workspace_root.resolve())

    async def get_connection(self, workspace_root: Path) -> LspConnection:
        """Get or create a connection for the workspace."""
        key = self._workspace_key(workspace_root)
        if key not in self._connections:
            conn = LspConnection(self.config, workspace_root)
            await conn.start()
            self._connections[key] = conn
        return self._connections[key]

    async def query(
        self,
        workspace_root: Path,
        operation: LspOperation,
        file_path: str,
        position: LspPosition,
    ) -> LspQueryResult:
        """Execute a query using the workspace's connection."""
        conn = await self.get_connection(workspace_root)
        return await conn.query(operation, file_path, position)

    async def shutdown_all(self) -> None:
        """Shutdown all connections."""
        for conn in self._connections.values():
            await conn.shutdown()
        self._connections.clear()


class LspService:
    """LSP service: manages provider registry and routes queries."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._providers: dict[str, LspProvider] = {}
        self._routes: dict[str, Route] = {}  # extension -> Route

    def register_provider(self, config: LspProviderConfig) -> None:
        """Register an LSP provider."""
        if config.id in self._providers:
            raise LspError(f"LSP provider '{config.id}' already registered", "LSP_CONFLICT")

        # Validate extensions
        for ext in config.extension_to_language:
            norm_ext = normalize_extension(ext)
            if norm_ext in self._routes:
                raise LspError(
                    f"Extension '{norm_ext}' already registered by another provider", "LSP_CONFLICT"
                )

        provider = LspProvider(config)
        self._providers[config.id] = provider

        # Register routes
        for ext, lang_id in config.extension_to_language.items():
            norm_ext = normalize_extension(ext)
            self._routes[norm_ext] = Route(provider=provider, language_id=lang_id)

    def unregister_provider(self, provider_id: str) -> None:
        """Unregister an LSP provider."""
        provider = self._providers.pop(provider_id, None)
        if provider:
            # Remove routes
            for ext, route in list(self._routes.items()):
                if route.provider == provider:
                    del self._routes[ext]

    async def query(
        self,
        workspace_root: Path,
        operation: LspOperation,
        file_path: str,
        position: LspPosition,
    ) -> LspQueryResult:
        """Route a query to the appropriate provider."""
        ext = final_extension(file_path)
        route = self._routes.get(ext)
        if not route:
            return LspQueryResult(
                operation=operation,
                error=f"No LSP provider registered for extension '{ext}'",
            )

        return await route.provider.query(workspace_root, operation, file_path, position)

    def get_provider(self, provider_id: str) -> LspProvider | None:
        return self._providers.get(provider_id)

    def list_providers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p.config.id,
                "extensions": list(p.config.extension_to_language.keys()),
                "command": p.config.command,
            }
            for p in self._providers.values()
        ]

    async def shutdown(self) -> None:
        """Shutdown all providers."""
        for provider in self._providers.values():
            await provider.shutdown_all()
        self._providers.clear()
        self._routes.clear()


# Global service instance (initialized from settings)
_lsp_service: LspService | None = None


def get_lsp_service(settings: Settings) -> LspService:
    """Get or create the global LSP service."""
    global _lsp_service
    if _lsp_service is None:
        _lsp_service = LspService(settings)
        # Register providers from settings
        lsp_servers = getattr(settings, "deepspace_lsp_servers", {})
        for name, server_config in lsp_servers.items():
            try:
                config = LspProviderConfig(
                    id=name,
                    command=server_config.get("command", []),
                    extension_to_language=server_config.get("extension_to_language", {}),
                    env=server_config.get("env", {}),
                    initialization_options=server_config.get("initialization_options"),
                    configuration=server_config.get("configuration"),
                )
                _lsp_service.register_provider(config)
            except LspError as e:
                logger.warning("Failed to register LSP provider '%s': %s", name, e)
    return _lsp_service


async def shutdown_lsp_service() -> None:
    """Shutdown the global LSP service."""
    global _lsp_service
    if _lsp_service:
        await _lsp_service.shutdown()
        _lsp_service = None

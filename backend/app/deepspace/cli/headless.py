"""Headless local run profile for DeepSpace.

Provides a CLI entry point that runs DeepSpace without a server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from app.auth.dependencies import AuthContext
from app.core.config import Settings, get_settings
from app.deepspace.cli.client import _parse_sse_frames
from app.deepspace.repositories.chat import DeepSpaceChatRepository
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.model_selection import ModelSelectionService
from app.platform.database.session import get_session_factory, set_db_tenant_context

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HeadlessConfig:
    """Headless profile configuration."""

    enabled: bool = True
    default_tenant: str | None = None
    default_user: str | None = None
    default_model: str | None = None
    max_turns: int = 100
    turn_timeout_seconds: int = 300
    output_format: Literal["json", "text", "jsonl"] = "json"
    show_reasoning: bool = False
    show_tool_calls: bool = True


# ──────────────────────────────────────────────────────────────────────
# Headless Runner
# ──────────────────────────────────────────────────────────────────────


@dataclass
class HeadlessTurnResult:
    """Result of a headless turn."""

    turn: int
    success: bool
    content: str
    tool_calls: list[dict[str, Any]]
    error: str | None = None
    metadata: dict[str, Any] | None = None
    reasoning: str = ""


def _render_result_payload(result: HeadlessTurnResult, config: HeadlessConfig) -> dict[str, Any]:
    """Build the public CLI payload according to the profile's output flags."""
    payload: dict[str, Any] = {
        "turn": result.turn,
        "success": result.success,
        "content": result.content,
        "error": result.error,
    }
    if config.show_tool_calls:
        payload["tool_calls"] = result.tool_calls
    if config.show_reasoning and result.reasoning:
        payload["reasoning"] = result.reasoning
    return payload


class HeadlessRunner:
    """Runs DeepSpace in headless mode."""

    def __init__(self, config: HeadlessConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self._chat_service: DeepSpaceChatService | None = None
        self._db: Any = None
        self._tenant_id: UUID | None = None
        self._user_id: UUID | None = None
        self._conversation_id: UUID | None = None

    async def initialize(self, tenant_id: UUID | None = None, user_id: UUID | None = None) -> None:
        """Initialize the headless runner."""
        # Parse tenant/user from config or use defaults
        self._tenant_id = tenant_id or (
            UUID(self.config.default_tenant) if self.config.default_tenant else uuid4()
        )
        self._user_id = user_id or (
            UUID(self.config.default_user) if self.config.default_user else uuid4()
        )

        # Create chat service
        session_factory = get_session_factory()
        self._db = session_factory()
        set_db_tenant_context(self._db, self._tenant_id)

        self._chat_service = DeepSpaceChatService(
            db=self._db,
            settings=self.settings,
        )

    async def run_turn(
        self,
        prompt: str,
        conversation_id: UUID | None = None,
        model: str | None = None,
        max_turns: int | None = None,
    ) -> HeadlessTurnResult:
        """Run a single turn."""

        if not self._chat_service:
            raise RuntimeError("Headless runner not initialized")

        # Use or create conversation
        if conversation_id is None:
            if self._db is None:
                raise RuntimeError("Headless runner database is not initialized")
            repo = DeepSpaceChatRepository(self._db)
            conv = repo.create_conversation(
                tenant_id=self._tenant_id,
                user_id=self._user_id,
                title="Headless Session",
            )
            self._db.commit()
            conversation_id = conv.id
        self._conversation_id = conversation_id

        requested_model = model or self.config.default_model
        if requested_model:
            if self._db is None:
                raise RuntimeError("Headless runner database is not initialized")
            routes = ModelSelectionService(self._db).routes(
                tenant_id=self._tenant_id,
                user_id=self._user_id,
            )
            matches = [route for route in routes if route.model_name == requested_model]
            configured_provider = str(getattr(self.settings, "llm_provider", "") or "").strip()
            configured_model = str(getattr(self.settings, "llm_model", "") or "").strip()
            settings_match = (
                not matches
                and configured_provider not in {"", "disabled"}
                and configured_model == requested_model
            )
            if not matches and not settings_match:
                raise ValueError(
                    f"Model '{requested_model}' is not available to this headless session"
                )
            if matches:
                ModelSelectionService(self._db).set_selection(
                    tenant_id=self._tenant_id,
                    user_id=self._user_id,
                    conversation_id=conversation_id,
                    provider_config_id=matches[0].provider_config_id,
                    model_name=matches[0].model_name,
                )

        # Create auth context
        auth = AuthContext(
            tenant_id=self._tenant_id,
            user_id=self._user_id,
            roles=frozenset({"user"}),
            token_id="headless",
        )

        # Run the turn via chat service.  The headless profile owns its
        # timeout so a CLI or SDK child cannot wait forever for a provider,
        # even when the HTTP server is not present to enforce a request
        # deadline.
        try:
            content_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            error: str | None = None
            turn_num = 1
            reasoning_parts: list[str] = []

            async with asyncio.timeout(self.config.turn_timeout_seconds):
                async for raw_frame in self._chat_service.stream_turn(
                    auth=auth,
                    conversation_id=conversation_id,
                    prompt=prompt,
                ):
                    frames = _parse_sse_frames(raw_frame) if isinstance(raw_frame, str) else []
                    for event, data in frames:
                        if event == "delta":
                            content_parts.append(str(data.get("text") or ""))
                        elif event == "tool_call":
                            tool_calls.append(data)
                        elif event in {"thinking", "reasoning", "reasoning_delta"}:
                            reasoning_parts.append(str(data.get("text") or ""))
                        elif event == "error":
                            error = str(data.get("message") or "Headless turn failed")
                        elif event == "turn_end":
                            turn_num = int(data.get("turn") or 1)

            content = "".join(content_parts)
            success = error is None

            return HeadlessTurnResult(
                turn=turn_num,
                success=success,
                content=content,
                tool_calls=tool_calls,
                error=error,
                metadata={"conversation_id": str(conversation_id)},
                reasoning="".join(reasoning_parts),
            )
        except TimeoutError:
            return HeadlessTurnResult(
                turn=turn_num,
                success=False,
                content="".join(content_parts),
                tool_calls=tool_calls,
                error=f"Headless turn timed out after {self.config.turn_timeout_seconds} seconds",
                metadata={"conversation_id": str(conversation_id)},
                reasoning="".join(reasoning_parts),
            )
        except Exception as e:
            logger.exception("Headless turn failed")
            return HeadlessTurnResult(
                turn=1,
                success=False,
                content="",
                tool_calls=[],
                error=str(e),
            )

    async def run_conversation(
        self,
        prompts: list[str],
        conversation_id: UUID | None = None,
        model: str | None = None,
    ) -> list[HeadlessTurnResult]:
        """Run a multi-turn conversation."""

        results = []
        for i, prompt in enumerate(prompts):
            if self.config.max_turns and i >= self.config.max_turns:
                break
            turn_result = await self.run_turn(prompt, conversation_id, model=model)
            results.append(turn_result)
            conversation_id = self._conversation_id
            if not turn_result.success:
                break
        return results

    async def shutdown(self) -> None:
        """Shutdown the runner."""
        if self._chat_service:
            self._chat_service = None
        if self._db is not None:
            self._db.close()
            self._db = None


async def run_sdk_minimal_stdio(
    *,
    runner: HeadlessRunner,
    input_stream: Any = sys.stdin,
    output_stream: Any = sys.stdout,
) -> int:
    """Serve one newline-delimited SDK-minimal session.

    The SDK subagent provider starts a short-lived child process, sends an
    ``initialize`` request followed by one ``turn`` request, and waits for the
    child to exit.  This adapter deliberately uses one JSON object per line;
    it is a distinct, documented profile protocol and does not mix framing
    with the Content-Length transport used by :mod:`app.deepspace.sdk.server`.

    Args:
        runner: An initialized headless runner.
        input_stream: A text stream carrying JSON-RPC request lines.
        output_stream: A text stream receiving JSON-RPC response lines.

    Returns:
        ``0`` after a clean protocol session, or ``1`` after malformed input.
    """

    def write(message: dict[str, Any]) -> None:
        output_stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        output_stream.flush()

    initialized = False
    while True:
        line = await asyncio.to_thread(input_stream.readline)
        if not line:
            return 0
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Invalid JSON: {exc.msg}"},
                }
            )
            return 1
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            write(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32600, "message": "A JSON-RPC 2.0 object is required."},
                }
            )
            return 1

        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "params must be an object."},
                }
            )
            return 1
        if method == "initialize":
            initialized = True
            write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": 1,
                        "serverInfo": {"name": "averqel-deepspace", "profile": "sdk-minimal"},
                        "capabilities": {"turn": True},
                    },
                }
            )
            continue
        if method == "shutdown":
            write({"jsonrpc": "2.0", "id": request_id, "result": {"status": "ok"}})
            return 0
        if method != "turn":
            write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
            continue
        if not initialized:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32006, "message": "initialize must be called first."},
                }
            )
            return 1
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "turn.prompt must not be empty."},
                }
            )
            return 1
        try:
            result = await runner.run_turn(prompt, model=params.get("model"))
        except Exception as exc:  # noqa: BLE001
            write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "status": "failed",
                        "answer": "",
                        "tool_calls": [],
                        "error": str(exc),
                    },
                }
            )
            return 1
        if result.content:
            write(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/update",
                    "params": {"text": result.content},
                }
            )
        for tool_call in result.tool_calls:
            write({"jsonrpc": "2.0", "method": "tool/call", "params": tool_call})
        write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "status": "completed" if result.success else "failed",
                    "answer": result.content,
                    "tool_calls": result.tool_calls,
                    "error": result.error,
                    "conversation_id": (result.metadata or {}).get("conversation_id"),
                },
            }
        )
        return 0 if result.success else 1


# ──────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ──────────────────────────────────────────────────────────────────────


def create_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="deepspace",
        description="DeepSpace headless runner",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="Prompt(s) to send to the agent",
    )
    parser.add_argument(
        "-c",
        "--conversation",
        help="Conversation ID to continue",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Model to use",
    )
    parser.add_argument(
        "-t",
        "--tenant",
        help="Tenant ID",
    )
    parser.add_argument(
        "-u",
        "--user",
        help="User ID",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Maximum turns per conversation (deployment default when omitted)",
    )
    parser.add_argument(
        "--format",
        choices=["json", "text", "jsonl"],
        default=None,
        help="Output format (deployment default when omitted)",
    )
    parser.add_argument(
        "--show-reasoning",
        action="store_true",
        default=None,
        help="Show reasoning in output",
    )
    parser.add_argument(
        "--show-tools",
        action="store_true",
        default=None,
        help="Show tool calls in output",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Interactive mode (read prompts from stdin)",
    )
    parser.add_argument(
        "--profile",
        choices=["direct", "sdk-minimal"],
        default="direct",
        help="Runtime profile; sdk-minimal serves newline JSON-RPC over stdin/stdout.",
    )
    return parser


async def main(args: argparse.Namespace) -> int:
    """Main entry point."""
    settings = get_settings()
    config = HeadlessConfig(
        enabled=getattr(settings, "deepspace_headless_enabled", True),
        default_tenant=args.tenant or getattr(settings, "deepspace_headless_default_tenant", None),
        default_user=args.user or getattr(settings, "deepspace_headless_default_user", None),
        default_model=args.model or getattr(settings, "deepspace_headless_default_model", None),
        max_turns=args.max_turns or getattr(settings, "deepspace_headless_max_turns", 100),
        turn_timeout_seconds=getattr(settings, "deepspace_headless_turn_timeout_seconds", 300),
        output_format=args.format or getattr(settings, "deepspace_headless_output_format", "json"),
        show_reasoning=(
            args.show_reasoning
            if args.show_reasoning is not None
            else getattr(settings, "deepspace_headless_show_reasoning", False)
        ),
        show_tool_calls=(
            args.show_tools
            if args.show_tools is not None
            else getattr(settings, "deepspace_headless_show_tools", True)
        ),
    )

    if not config.enabled:
        print("DeepSpace headless mode is disabled by deployment settings.", file=sys.stderr)
        return 1

    runner = HeadlessRunner(config, settings)
    tenant_id = UUID(args.tenant) if args.tenant else None
    user_id = UUID(args.user) if args.user else None
    await runner.initialize(tenant_id, user_id)

    try:
        if args.profile == "sdk-minimal":
            return await run_sdk_minimal_stdio(runner=runner)
        if args.interactive:
            # Interactive mode
            conversation_id = UUID(args.conversation) if args.conversation else None
            print("DeepSpace Headless Interactive Mode (Ctrl+C to exit)")
            while True:
                try:
                    prompt = input("> ").strip()
                    if not prompt:
                        continue
                    result = await runner.run_turn(prompt, conversation_id)
                    if args.format == "json":
                        print(json.dumps(_render_result_payload(result, config)))
                    elif args.format == "jsonl":
                        print(json.dumps(_render_result_payload(result, config)))
                    else:
                        if result.success:
                            print(result.content)
                            if config.show_reasoning and result.reasoning:
                                print(f"Reasoning: {result.reasoning}")
                            if config.show_tool_calls and result.tool_calls:
                                print(f"Tool calls: {len(result.tool_calls)}")
                        else:
                            print(f"Error: {result.error}", file=sys.stderr)
                except EOFError:
                    break
                except KeyboardInterrupt:
                    break
        elif args.prompt:
            # Single or multi-prompt mode
            prompts = args.prompt
            results = await runner.run_conversation(prompts, model=config.default_model)
            for turn_result in results:
                if args.format == "json":
                    print(json.dumps(_render_result_payload(turn_result, config)))
                elif args.format == "jsonl":
                    print(json.dumps(_render_result_payload(turn_result, config)))
                else:
                    if turn_result.success:
                        print(turn_result.content)
                        if config.show_reasoning and turn_result.reasoning:
                            print(f"Reasoning: {turn_result.reasoning}")
                        if config.show_tool_calls and turn_result.tool_calls:
                            print(f"Tool calls: {len(turn_result.tool_calls)}")
                    else:
                        print(f"Error: {turn_result.error}", file=sys.stderr)
        else:
            print("No prompt provided. Use --interactive or provide prompts.", file=sys.stderr)
            return 1

        return 0
    finally:
        await runner.shutdown()


def cli_entry() -> int:
    """CLI entry point for setuptools."""
    parser = create_parser()
    args = parser.parse_args()
    return asyncio.run(main(args))


if __name__ == "__main__":
    raise SystemExit(cli_entry())


__all__ = [
    "HeadlessConfig",
    "HeadlessRunner",
    "HeadlessTurnResult",
    "run_sdk_minimal_stdio",
    "create_parser",
    "main",
    "cli_entry",
]

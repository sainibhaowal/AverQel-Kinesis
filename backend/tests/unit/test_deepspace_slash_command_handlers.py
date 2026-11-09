from __future__ import annotations

import uuid

import pytest

from app.deepspace.services.slash_commands import (
    FunctionSlashCommand,
    SlashCommandArg,
    SlashCommandContext,
    SlashCommandResult,
)

pytestmark = pytest.mark.unit_no_db


def _context() -> SlashCommandContext:
    return SlashCommandContext(
        conversation_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        raw_input="/example",
        command="example",
        args=[SlashCommandArg(name="arg0", value="value")],
        kwargs={},
    )


@pytest.mark.asyncio
async def test_function_slash_command_accepts_sync_and_async_handlers() -> None:
    sync_command = FunctionSlashCommand(
        name="sync",
        func=lambda context: SlashCommandResult(
            success=True,
            message=context.args[0].value,
        ),
    )

    async def async_handler(context: SlashCommandContext) -> SlashCommandResult:
        return SlashCommandResult(success=True, message=context.args[0].value)

    async_command = FunctionSlashCommand(name="async", func=async_handler)

    assert (await sync_command.execute(_context())).message == "value"
    assert (await async_command.execute(_context())).message == "value"

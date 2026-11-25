import asyncio

from app.deepspace.services.execution_batches import plan_execution_batches


def test_execution_batches_keep_writes_as_ordering_barriers() -> None:
    calls = [{"id": "read-1"}, {"id": "read-2"}, {"id": "write"}, {"id": "read-3"}]

    batches = plan_execution_batches(calls, is_read=lambda call: call["id"].startswith("read"))

    assert [(batch.parallel, [call["id"] for call in batch.calls]) for batch in batches] == [
        (True, ["read-1", "read-2"]),
        (False, ["write"]),
        (True, ["read-3"]),
    ]


def test_parallel_batch_can_be_awaited_without_reordering_inputs() -> None:
    async def run() -> list[str]:
        calls = [{"id": "read-1"}, {"id": "read-2"}]
        batch = plan_execution_batches(calls, is_read=lambda _: True)[0]
        return await asyncio.gather(*(asyncio.sleep(0, result=call["id"]) for call in batch.calls))

    assert asyncio.run(run()) == ["read-1", "read-2"]


def test_subagent_and_subagent_fork_join_parallel_batches_while_writes_stay_barriers() -> None:
    from app.deepspace.services.chat_service import DeepSpaceChatService

    calls = [
        {"call_id": "c1", "tool_name": "subagent", "arguments": {"description": "A", "task": "1"}},
        {
            "call_id": "c2",
            "tool_name": "subagent_fork",
            "arguments": {"description": "B", "task": "2"},
        },
        {"call_id": "c3", "tool_name": "read", "arguments": {"path": "README.md"}},
        {"call_id": "c4", "tool_name": "write", "arguments": {"path": "out.txt", "content": "hi"}},
        {
            "call_id": "c5",
            "tool_name": "get_agent",
            "arguments": {"agent_id": "a1", "wait_timeout_seconds": 5},
        },
        {"call_id": "c6", "tool_name": "subagent", "arguments": {"description": "C", "task": "3"}},
    ]

    batches = DeepSpaceChatService._plan_tool_execution_batches(calls, mcp_bindings={})

    assert [(batch.parallel, [call["call_id"] for call in batch.calls]) for batch in batches] == [
        (True, ["c1", "c2", "c3"]),
        (False, ["c4"]),
        (False, ["c5"]),
        (True, ["c6"]),
    ]


def test_team_spawn_and_message_join_parallel_batches_while_task_update_and_interrupt_stay_barriers() -> (
    None
):
    from app.deepspace.services.chat_service import DeepSpaceChatService

    calls = [
        {"call_id": "t1", "tool_name": "spawn_teammate", "arguments": {"name": "scout-a"}},
        {"call_id": "t2", "tool_name": "spawn_teammate", "arguments": {"name": "scout-b"}},
        {
            "call_id": "t3",
            "tool_name": "team_send_message",
            "arguments": {"to": "scout-a", "body": "go"},
        },
        {
            "call_id": "t4",
            "tool_name": "team_task_update",
            "arguments": {"task_id": "x", "revision": 1},
        },
        {"call_id": "t5", "tool_name": "interrupt_agent", "arguments": {"target": "scout-b"}},
    ]

    batches = DeepSpaceChatService._plan_tool_execution_batches(calls, mcp_bindings={})

    assert [(batch.parallel, [call["call_id"] for call in batch.calls]) for batch in batches] == [
        (True, ["t1", "t2", "t3"]),
        (False, ["t4"]),
        (False, ["t5"]),
    ]

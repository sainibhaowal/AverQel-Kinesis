"""Tests for the agent mode a conversation runs in.

The mode is a named set of rules: which tools the agent may call, how deep
it may delegate, which boundary it runs in, how hard it thinks, and whether
it may call the tools from a program.  The custom mode carries the settings
the user edited, so they are stored, read back, and applied.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.deepspace.models.agent_team import TEAM_MEMBER_STATE_IDLE, TeamMember
from app.deepspace.models.conversation import Conversation
from app.deepspace.services.chat_service import (
    NATIVE_TOOL_REGISTRY,
    DeepSpaceChatService,
    _TurnState,
)
from app.deepspace.services.presets import (
    PresetSettingsError,
    PresetStateService,
    custom_preset_from_settings,
    resolve_preset,
    strictest_sandbox_mode,
    user_default_conversation_id,
)
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.subagent_runner import child_tool_names
from app.deepspace.services.tools import dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite
from app.providers.services.types import ChatGenerateResponse

pytestmark = pytest.mark.unit_no_db


class StubProvider:
    """A provider that answers with a scripted sequence of responses."""

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[Any] = []

    def generate(self, request: Any) -> ChatGenerateResponse:
        self.requests.append(request)
        payload = self.script.pop(0) if self.script else {"content": "done"}
        return ChatGenerateResponse(
            content=str(payload.get("content") or ""),
            tool_calls=payload.get("tool_calls"),
        )


@pytest.fixture(scope="module")
def conversation_database(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Build the database the agent-mode checks share."""
    tmp_path = tmp_path_factory.mktemp("agent-mode")
    settings = Settings(
        deepspace_workspace_root=str(tmp_path / "workspaces"),
        deepspace_subagent_max_depth=3,
        deepspace_subagent_max_children=4,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'agent-mode.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    return engine, settings


@pytest.fixture
def conversation(conversation_database: Any) -> Any:
    """Build a context and a provider for one conversation."""
    engine, settings = conversation_database
    session = sessionmaker(bind=engine)()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kind="deepspace",
            title="Agent mode test",
        )
    )
    session.commit()

    provider = StubProvider()
    candidate = SimpleNamespace(
        provider_type="stub",
        model_name="stub-model",
        base_url="http://stub.invalid",
        api_key="stub-key",
    )

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "echo": arguments}

    context = ToolContext(
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        conversation_id=conversation_id,
        settings=settings,
        db=session,
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(
            get_chat_provider_from_selection=lambda _candidate: provider
        ),
        provider_selection=SimpleNamespace(
            resolve_chat=lambda **_: SimpleNamespace(candidates=[candidate])
        ),
        native_executor=executor,
    )
    yield {
        "context": context,
        "session": session,
        "provider": provider,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "settings": settings,
    }
    session.close()


class _RecordedSandboxMode:
    """A sandbox-mode service that reports one recorded mode."""

    def __init__(self, recorded: str | None) -> None:
        self.recorded = recorded

    def recorded_mode(self, **_kwargs: Any) -> str | None:
        return self.recorded


def _chat_service(settings: Settings, recorded_mode: str | None = None) -> DeepSpaceChatService:
    """Build a chat service with only the parts the agent mode needs."""
    service = object.__new__(DeepSpaceChatService)
    service.settings = settings
    service.db = None
    service.tool_policy = DeepSpaceToolPolicy()
    service.sandbox_mode = _RecordedSandboxMode(recorded_mode)
    return service


def _turn_state(**overrides: Any) -> _TurnState:
    """Build a turn state for the agent-mode checks."""
    state = _TurnState(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        native_tool_names={"fs_read", "fs_write", "run_code", "bash"},
        reasoning_effort=None,
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_the_standard_mode_withholds_programmatic_calling() -> None:
    settings = Settings()
    service = _chat_service(settings)
    state = _turn_state()

    service._apply_agent_mode(state, resolve_preset("standard", settings=settings), {})

    assert "run_code" not in state.native_tool_names
    assert "fs_write" in state.native_tool_names


def test_the_programmatic_calling_mode_keeps_the_program_tool() -> None:
    settings = Settings()
    service = _chat_service(settings)
    state = _turn_state()

    service._apply_agent_mode(state, resolve_preset("ptc", settings=settings), {})

    assert "run_code" in state.native_tool_names
    assert state.reasoning_effort == "medium"


def test_the_deployment_can_disable_programmatic_calling_for_every_mode() -> None:
    settings = Settings(deepspace_ptc_enabled=False)
    service = _chat_service(settings)
    state = _turn_state()

    service._apply_agent_mode(state, resolve_preset("ptc", settings=settings), {})

    assert "run_code" not in state.native_tool_names
    assert resolve_preset("ptc", settings=settings).ptc_enabled is False


def test_a_custom_mode_can_start_a_turn_in_plan_mode() -> None:
    settings = Settings()
    service = _chat_service(settings)
    state = _turn_state(plan_mode_active=False, plan_mode_recorded=None)
    resolution = resolve_preset(
        "custom",
        settings=settings,
        custom_settings={"plan_mode_by_default": True},
    )

    service._apply_agent_mode(state, resolution, {})

    assert state.plan_mode_active is True


def test_an_explicit_plan_mode_choice_wins_over_the_preset_default() -> None:
    settings = Settings()
    service = _chat_service(settings)
    state = _turn_state(plan_mode_active=False, plan_mode_recorded=False)
    resolution = resolve_preset(
        "custom",
        settings=settings,
        custom_settings={"plan_mode_by_default": True},
    )

    service._apply_agent_mode(state, resolution, {})

    assert state.plan_mode_active is False


def test_the_mode_supplies_the_reasoning_effort_the_request_left_out() -> None:
    settings = Settings()
    service = _chat_service(settings)
    state = _turn_state(reasoning_effort="low")

    service._apply_agent_mode(state, resolve_preset("ptc", settings=settings), {})

    assert state.reasoning_effort == "low"


def test_the_minimal_mode_runs_read_only() -> None:
    settings = Settings()
    service = _chat_service(settings)
    state = _turn_state()

    service._apply_agent_mode(state, resolve_preset("minimal", settings=settings), {})

    assert state.sandbox_mode == "read-only"
    assert "fs_write" not in state.native_tool_names
    assert "bash" not in state.native_tool_names
    assert "fs_read" in state.native_tool_names


def test_the_strictest_of_the_modes_is_the_mode_a_turn_runs_in() -> None:
    settings = Settings(deepspace_sandbox_policy="workspace-write")
    service = _chat_service(settings, recorded_mode="read-only")
    state = _turn_state()

    service._apply_agent_mode(state, resolve_preset("standard", settings=settings), {})

    assert state.sandbox_mode == "read-only"


def test_a_conversation_cannot_widen_the_deployment_boundary() -> None:
    settings = Settings(deepspace_sandbox_policy="workspace-write")
    service = _chat_service(settings, recorded_mode="danger-full-access")
    state = _turn_state()

    service._apply_agent_mode(state, resolve_preset("standard", settings=settings), {})

    assert state.sandbox_mode == "workspace-write"


def test_the_strictest_sandbox_mode_is_the_tightest_one() -> None:
    assert strictest_sandbox_mode("danger-full-access", "read-only") == "read-only"
    assert strictest_sandbox_mode("workspace-write", "read-only") == "read-only"
    assert strictest_sandbox_mode("workspace-write", "danger-full-access") == "workspace-write"
    assert strictest_sandbox_mode(None, None) is None


def test_a_preset_may_only_narrow_the_deployment_depth() -> None:
    settings = SimpleNamespace(deepspace_subagent_max_depth=1)

    assert resolve_preset("standard", settings=settings).subagent_max_depth == 1
    assert resolve_preset("ptc", settings=settings).subagent_max_depth == 1


def test_a_custom_mode_settings_are_stored_and_read_back(conversation: Any) -> None:
    service = PresetStateService(conversation["session"])
    settings = {
        "name": "Research",
        "tools": ["fs_read", "web_search"],
        "sandbox_mode": "read-only",
        "reasoning_effort": "high",
        "subagent_max_depth": 1,
        "ptc_enabled": False,
    }

    service.set_preset(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        preset_id="custom",
        custom_settings=settings,
    )
    recorded = service.recorded_preset_settings(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
    )

    assert recorded == settings
    assert (
        service.recorded_preset(
            tenant_id=conversation["tenant_id"],
            user_id=conversation["user_id"],
            conversation_id=conversation["conversation_id"],
        )
        == "custom"
    )


def test_a_custom_mode_settings_are_applied_to_the_turn(conversation: Any) -> None:
    from app.deepspace.services.presets import effective_preset_resolution

    service = PresetStateService(conversation["session"])
    service.set_preset(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        preset_id="custom",
        custom_settings={
            "tools": ["fs_read", "web_search"],
            "sandbox_mode": "read-only",
            "reasoning_effort": "high",
        },
    )

    resolution = effective_preset_resolution(
        db=conversation["session"],
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        settings=conversation["settings"],
    )

    assert resolution.preset.id == "custom"
    assert resolution.reasoning_effort == "high"
    assert resolution.sandbox_mode == "read-only"
    assert "fs_write" not in (resolution.tool_allowlist or ())
    assert "fs_read" in (resolution.tool_allowlist or ())


def test_a_conversation_custom_selection_inherits_the_saved_custom_tools(
    conversation: Any,
) -> None:
    """A bare Custom selection must not widen the user's saved tool list."""
    from app.deepspace.services.presets import effective_preset_resolution

    service = PresetStateService(conversation["session"])
    settings = {"tools": ["fs_read"]}
    service.set_preset(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=user_default_conversation_id(
            conversation["tenant_id"], conversation["user_id"]
        ),
        preset_id="custom",
        custom_settings=settings,
    )
    service.set_preset(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        preset_id="custom",
    )

    resolution = effective_preset_resolution(
        db=conversation["session"],
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        settings=conversation["settings"],
    )

    assert resolution.tool_allowlist is not None
    assert set(resolution.tool_allowlist) == {"fs_read", "discover_tools", "get_tool_result"}


def test_a_custom_mode_settings_the_product_does_not_accept_are_refused() -> None:
    with pytest.raises(PresetSettingsError):
        custom_preset_from_settings({"sandbox_mode": "root"})
    with pytest.raises(PresetSettingsError):
        custom_preset_from_settings({"reasoning_effort": "extreme"})
    with pytest.raises(PresetSettingsError):
        custom_preset_from_settings({"subagent_max_depth": -1})
    with pytest.raises(PresetSettingsError):
        custom_preset_from_settings({"tools": "fs_read"})


def test_a_built_in_mode_does_not_accept_custom_settings() -> None:
    service = PresetStateService(SimpleNamespace())

    with pytest.raises(PresetSettingsError, match="only be supplied"):
        service.set_preset(
            tenant_id="tenant",
            user_id="user",
            conversation_id="conversation",
            preset_id="standard",
            custom_settings={"ptc_enabled": True},
        )


def test_a_child_inherits_the_parents_mode(conversation: Any) -> None:
    service = PresetStateService(conversation["session"])
    service.set_preset(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        preset_id="custom",
        custom_settings={"tools": ["fs_read", "web_search"]},
    )
    provider = conversation["provider"]
    provider.script = [{"content": "The child reports 7."}]

    import asyncio

    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "work", "mode": "continuable"},
            conversation["context"],
        )
    )
    offered = {tool["function"]["name"] for tool in (provider.requests[0].tools or [])}

    assert "fs_read" in offered
    assert "fs_write" not in offered
    assert "bash" not in offered


def test_a_child_never_receives_the_team_tools_or_the_question_tool(
    conversation: Any,
) -> None:
    provider = conversation["provider"]
    provider.script = [{"content": "The child reports 7."}]

    import asyncio

    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "work", "mode": "continuable"},
            conversation["context"],
        )
    )
    offered = {tool["function"]["name"] for tool in (provider.requests[0].tools or [])}

    assert "ask_user" not in offered
    assert "team_send_message" not in offered
    assert "wait_agent" not in offered
    assert "spawn_teammate" not in offered


def test_a_teammate_keeps_the_team_tools(conversation: Any) -> None:
    session = conversation["session"]
    teammate = TeamMember(
        id=uuid.uuid4(),
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        name="scout",
        child_id=uuid.uuid4(),
        state=TEAM_MEMBER_STATE_IDLE,
    )
    session.add(teammate)
    session.commit()
    child = SimpleNamespace(
        id=teammate.child_id,
        tool_filter={},
        delegation_depth=1,
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
    )

    names = child_tool_names(
        NATIVE_TOOL_REGISTRY,
        child,
        max_depth=3,
        teammate=True,
    )
    assert "team_task_list" in names
    assert "team_send_message" in names


def test_a_child_cannot_delegate_past_the_mode_depth(conversation: Any) -> None:
    service = PresetStateService(conversation["session"])
    service.set_preset(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        preset_id="minimal",
    )

    import asyncio

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "work", "mode": "continuable"},
            conversation["context"],
        )
    )

    assert result is not None
    assert result["status"] == "denied"
    assert result["max_depth"] == 0

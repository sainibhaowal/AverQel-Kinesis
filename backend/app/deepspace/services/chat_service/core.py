"""Core DeepSpaceChatService class composing the chat_service mixins."""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import Settings
from app.deepspace.repositories.chat import DeepSpaceChatRepository
from app.deepspace.repositories.context_summaries import DeepSpaceContextSummaryRepository
from app.deepspace.repositories.request_metrics import DeepSpaceRequestMetricsRepository
from app.deepspace.services.chat_service.context import _ContextMixin
from app.deepspace.services.chat_service.events import _EventsMixin
from app.deepspace.services.chat_service.goals import _GoalsMixin
from app.deepspace.services.chat_service.provider import _ProviderMixin
from app.deepspace.services.chat_service.tools import _ToolsMixin
from app.deepspace.services.chat_service.turns import _TurnsMixin
from app.deepspace.services.chat_service.utils import (
    _pkg_attr,
)
from app.deepspace.services.chat_service.workspace import _WorkspaceMixin
from app.deepspace.services.context_budget import AdaptiveContextBudget
from app.deepspace.services.context_cache import DeepSpaceContextCache
from app.deepspace.services.context_epoch import DeepSpaceContextEpochStore
from app.deepspace.services.mcp_bridge import DeepSpaceMCPBridge
from app.deepspace.services.mcp_result_store import MCPResultStore
from app.deepspace.services.mcp_tool_broker import (
    MCPToolBroker,
)
from app.deepspace.services.media_artifacts import DeepSpaceMediaArtifactService
from app.deepspace.services.native_tool_broker import (
    NativeToolBroker,
)
from app.deepspace.services.plan_mode import (
    PlanModeService,
)
from app.deepspace.services.provider_circuit import DeepSpaceProviderCircuit
from app.deepspace.services.runtime_invariants import (
    get_invariant_registry,
)
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.runtime_store import DeepSpaceRuntimeStore
from app.deepspace.services.sandbox_mode import SandboxModeService
from app.deepspace.services.session_events import (
    SessionEventStore,
)
from app.deepspace.services.session_telemetry import get_session_telemetry
from app.deepspace.services.task_loop import (
    DeepSpaceTaskLoopStore,
)
from app.deepspace.services.time_context import (
    TimeContextService,
)
from app.deepspace.services.tool_pipeline import DeepSpaceToolPipeline
from app.deepspace.services.tool_result_store import ToolResultStore
from app.deepspace.services.tools import (
    NATIVE_TOOL_DEADLINES,
)
from app.deepspace.services.workspaces import WorkspaceSelectionService
from app.providers.services import ProviderRegistry
from app.providers.services.provider_secret_crypto import (
    ProviderSecretCrypto,
)
from app.providers.services.selection_service import ProviderSelectionService

logger = logging.getLogger(__name__)


class DeepSpaceChatService(
    _TurnsMixin,
    _ToolsMixin,
    _ProviderMixin,
    _ContextMixin,
    _GoalsMixin,
    _EventsMixin,
    _WorkspaceMixin,
):
    """Orchestrates DeepSpace turns, tool execution, context budgeting, and persistence."""

    settings: Any = None

    def __init__(self, *, db: Any, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.chat = _pkg_attr("DeepSpaceChatRepository", DeepSpaceChatRepository)(db)
        self._provider_secret_crypto: ProviderSecretCrypto | None = None
        self.context_summaries = DeepSpaceContextSummaryRepository(db)
        self.request_metrics = DeepSpaceRequestMetricsRepository(db)
        self.context_cache = DeepSpaceContextCache()
        self.context_budget = AdaptiveContextBudget()
        self.context_epochs = DeepSpaceContextEpochStore()
        self.provider_circuit = DeepSpaceProviderCircuit()
        self.providers = _pkg_attr("ProviderSelectionService", ProviderSelectionService)(
            db, settings
        )
        self.registry = _pkg_attr("ProviderRegistry", ProviderRegistry)(settings)
        self.task_store = _pkg_attr("DeepSpaceTaskLoopStore", DeepSpaceTaskLoopStore)(db)
        self.media_artifacts = DeepSpaceMediaArtifactService(db, settings)
        self.mcp_bridge = DeepSpaceMCPBridge(db, settings)
        self.mcp_broker = MCPToolBroker(
            max_search_results=int(getattr(settings, "deepspace_mcp_max_search_results", 5)),
            max_schema_chars=int(getattr(settings, "deepspace_mcp_max_schema_chars", 6_000)),
            max_result_chars=int(getattr(settings, "deepspace_mcp_max_result_chars", 12_000)),
        )
        self.native_tool_broker = NativeToolBroker()
        self.mcp_result_store = MCPResultStore()
        self.tool_result_store = ToolResultStore()
        self.session_events = _pkg_attr("SessionEventStore", SessionEventStore)(db)
        self.session_telemetry = get_session_telemetry(settings)
        self.time_context_service = TimeContextService()
        self.invariant_registry = get_invariant_registry(settings)
        self.mcp_max_calls_per_turn = int(getattr(settings, "deepspace_mcp_max_calls_per_turn", 8))
        self.mcp_max_discovery_calls_per_turn = int(
            getattr(settings, "deepspace_mcp_max_discovery_calls_per_turn", 8)
        )
        self.mcp_max_result_chars_per_turn = int(
            getattr(settings, "deepspace_mcp_max_result_chars_per_turn", 60_000)
        )
        self.mcp_max_argument_chars_per_call = int(
            getattr(settings, "deepspace_mcp_max_argument_chars_per_call", 20_000)
        )
        self.runtime = DeepSpaceRuntimeStore(
            db,
            retained_steps=int(getattr(settings, "deepspace_agent_retained_steps", 10_000)),
        )
        self.plan_mode = PlanModeService(db)
        self.sandbox_mode = SandboxModeService(db)
        self.workspace_selection = WorkspaceSelectionService(db)
        # Initialize goal round driver
        from app.deepspace.services.goal_driver import GoalRoundDriver

        self.goal_driver = GoalRoundDriver(self, settings)
        # Initialize session title service
        from app.deepspace.services.session_titles import SessionTitleService

        self.session_title_service = SessionTitleService(
            settings=settings,
            provider_registry=self.registry,
            circuit=self.provider_circuit,
        )
        self.tool_policy = DeepSpaceToolPolicy(
            approval_mode=str(getattr(settings, "deepspace_approval_mode", "never")),
            permission_mode=str(
                getattr(settings, "deepspace_sandbox_policy", None)
                or getattr(settings, "deepspace_permission_mode", "workspace-write")
            ),
        )
        # Initialize out-of-process subagent providers
        from app.deepspace.services.subagent_providers import initialize_providers_from_settings

        initialize_providers_from_settings(settings)
        self._tool_pipeline = DeepSpaceToolPipeline(
            self.tool_policy, settings=settings, tool_timeouts=NATIVE_TOOL_DEADLINES
        )
        # The deployment's hooks may refuse a tool call before it runs.
        self._load_deployment_hooks()
        self._hook_conversation_id = ""
        self._hook_auth = None
        self._tool_pipeline.register_pre("deepspace-hooks", self._hook_participant())

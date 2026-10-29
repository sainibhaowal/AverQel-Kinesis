"""Goal round driver for DeepSpace.

Ported from NeoSIS @averqel/neosis-goal-round-driver.
Manages automatic goal continuation rounds with race fences.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.deepspace.models.goal import DeepSpaceGoal, GoalPhase
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.turn_queue import DeepSpaceTurnQueueStore
from app.platform.database.session import get_session_factory, set_db_tenant_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RoundIdentity:
    """Identity reserved before a goal continuation enters the agent inbox."""

    goal_id: UUID
    revision: int
    round: int


@dataclass
class RoundAttempt:
    """One queued, claimed, or admitted goal message retained until whole-agent quiescence."""

    goal_id: UUID
    revision: int
    round: int
    message_id: UUID
    content: list[dict]
    phase: Literal[queued, claimed, admitted] = "queued"
    cancelled: bool = False
    stale: bool = False


@dataclass
class DriverState:
    """Serialized process-local scheduling state for one exact Agent lifecycle."""

    agent_id: UUID
    attempt: RoundAttempt | None = None
    competing_queued: bool = False
    needs_checkpoint: bool = False
    requested: bool = False
    run_task: asyncio.Task | None = None
    stopping: bool = False


def is_goal_round_source(source: dict[str, Any]) -> bool:
    """Whether a source identifies an automatic, positive-numbered goal round."""
    return source.get("kind") == "goal" and source.get("round", 0) > 0


def same_round(source: dict[str, Any], round_identity: RoundIdentity) -> bool:
    """Compare a source to one reserved identity."""
    return (
        source.get("goalId") == str(round_identity.goal_id)
        and source.get("revision") == round_identity.revision
        and source.get("round") == round_identity.round
    )


def same_queued(content: list[dict], source: dict[str, Any], attempt: RoundAttempt) -> bool:
    """Compare the complete queued record to the driver's reservation."""
    return is_goal_round_source(source) and same_round(source, attempt)


def goal_ref(goal: DeepSpaceGoal) -> dict[str, Any]:
    """Exact current ref for a view."""
    return {"id": str(goal.id), "revision": goal.revision}


class GoalRoundDriver:
    """Manages automatic same-session goal continuation and its race fences."""

    def __init__(
        self,
        chat_service: DeepSpaceChatService,
        settings: Settings,
    ):
        self.chat_service = chat_service
        self.settings = settings
        self._states: dict[UUID, DriverState] = {}  # agent_id -> DriverState
        self._enabled = getattr(settings, "deepspace_goal_driver_enabled", True)
        self._max_rounds_default = getattr(settings, "deepspace_goal_max_rounds_default", 3)

    def _get_queue_store(self, tenant_id: UUID, user_id: UUID) -> DeepSpaceTurnQueueStore:
        """Get a turn queue store for the conversation."""
        session_factory = get_session_factory()
        db = session_factory()
        set_db_tenant_context(db, tenant_id)
        return DeepSpaceTurnQueueStore(db)

    async def maybe_continue_goal(
        self,
        conversation_id: UUID,
        goal: DeepSpaceGoal,
        agent_id: UUID,
        auth: AuthContext,
        db: Any,
    ) -> bool:
        """Check if a goal should continue and queue the next round.

        Returns True if a continuation was queued.
        """
        if not self._enabled:
            return False

        # Check if goal is in a continuable phase
        if goal.phase != GoalPhase.ACTIVE:
            return False

        # Check round limit
        if goal.rounds_started >= goal.max_goal_rounds:
            # Goal has reached its round limit, mark as complete
            await self._complete_goal(goal, db)
            return False

        # Get driver state for this agent
        state = self._get_state(agent_id)

        # Check if there's already a queued/claimed round
        if state.attempt is not None:
            if state.attempt.cancelled or state.attempt.stale:
                state.attempt = None
            else:
                return False  # Already have a pending round

        # Reserve the next round
        next_round = goal.rounds_started + 1
        round_identity = RoundIdentity(
            goal_id=goal.id,
            revision=goal.revision,
            round=next_round,
        )

        # Create the continuation message
        message_id = uuid.uuid4()
        continuation_content = self._build_continuation_message(goal, next_round)

        state.attempt = RoundAttempt(
            goal_id=goal.id,
            revision=goal.revision,
            round=next_round,
            message_id=message_id,
            content=continuation_content,
            phase="queued",
        )

        # Add the continuation message to the conversation
        from app.deepspace.repositories.chat import DeepSpaceChatRepository

        repo = DeepSpaceChatRepository(db)
        repo.add_message(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            role="user",
            content=continuation_content[0]["text"] if continuation_content else "",
            metadata_json={
                "source": {
                    "kind": "goal",
                    "goalId": str(goal.id),
                    "revision": goal.revision,
                    "round": next_round,
                },
                "client_request_id": str(message_id),
            },
        )
        db.commit()

        # Enqueue the next turn in the queue
        queue_store = DeepSpaceTurnQueueStore(db)
        client_request_id = str(message_id)
        queue_store.enqueue(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            prompt=continuation_content[0]["text"] if continuation_content else "",
            thinking_enabled=False,
            reasoning_effort=None,
            roles=sorted(auth.roles),
            permissions=sorted(auth.permissions),
            attachment_file_ids=[],
            steer=False,
        )
        db.commit()

        # Increment goal rounds_started
        goal.rounds_started = next_round
        goal.revision += 1
        db.commit()

        logger.info(
            "Queued goal round %d for goal %s (conversation %s)",
            next_round,
            goal.id,
            conversation_id,
        )

        return True

    def _build_continuation_message(
        self,
        goal: DeepSpaceGoal,
        round_num: int,
    ) -> list[dict]:
        """Build the continuation message content."""
        return [
            {
                "type": "text",
                "text": f"Continuing goal '{goal.name}' (round {round_num}/{goal.max_goal_rounds}):\n{goal.content}",
            }
        ]

    def _get_state(self, agent_id: UUID) -> DriverState:
        """Get or create driver state for an agent."""
        if agent_id not in self._states:
            self._states[agent_id] = DriverState(agent_id=agent_id)
        return self._states[agent_id]

    async def _complete_goal(self, goal: DeepSpaceGoal, db: Any) -> None:
        """Mark a goal as complete."""
        goal.phase = GoalPhase.COMPLETE
        goal.updated_at = datetime.now(UTC)
        db.commit()

    def claim_attempt(self, agent_id: UUID, message_id: UUID, source: dict) -> RoundAttempt | None:
        """Claim a queued attempt when the agent processes it."""
        state = self._get_state(agent_id)
        if state.attempt is None:
            return None

        if not same_round(
            source,
            RoundIdentity(
                goal_id=state.attempt.goal_id,
                revision=state.attempt.revision,
                round=state.attempt.round,
            ),
        ):
            return None

        if state.attempt.message_id != message_id:
            return None

        state.attempt.phase = "claimed"
        return state.attempt

    def admit_attempt(self, agent_id: UUID) -> RoundAttempt | None:
        """Mark a claimed attempt as admitted (agent is processing it)."""
        state = self._get_state(agent_id)
        if state.attempt and state.attempt.phase == "claimed":
            state.attempt.phase = "admitted"
            return state.attempt
        return None

    def complete_attempt(self, agent_id: UUID) -> RoundAttempt | None:
        """Complete the current attempt and clear it."""
        state = self._get_state(agent_id)
        attempt = state.attempt
        if attempt:
            state.attempt = None
        return attempt

    def cancel_attempt(self, agent_id: UUID) -> None:
        """Cancel the current attempt."""
        state = self._get_state(agent_id)
        if state.attempt:
            state.attempt.cancelled = True
            state.attempt = None

    def cleanup_agent(self, agent_id: UUID) -> None:
        """Clean up state for a terminated agent."""
        self._states.pop(agent_id, None)


async def run_goal_continuation_turn(
    chat_service: DeepSpaceChatService,
    driver: GoalRoundDriver,
    conversation_id: UUID,
    goal: DeepSpaceGoal,
    agent_id: UUID,
) -> None:
    """Run a goal continuation turn.

    This is called when the agent's turn comes up and there's a queued goal round.
    """
    state = driver._get_state(agent_id)
    if state.attempt is None or state.attempt.phase != "queued":
        return

    # The agent will process the queued message on its next turn
    # We just need to ensure the message is in the conversation history
    logger.info(
        "Goal round %d ready for agent %s",
        state.attempt.round,
        agent_id,
    )

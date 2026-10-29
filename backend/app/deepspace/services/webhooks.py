"""Webhook ingress for DeepSpace.

Ported from NeoSIS @averqel/neosis-webhook and @averqel/neosis-webhook-github.
Provides fire-and-forget webhook rule registry and signed GitHub adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from app.core.config import Settings

logger = logging.getLogger(__name__)


# ============================================================================
# Core types
# ============================================================================


@dataclass(frozen=True, slots=True)
class VerifiedWebhookDelivery:
    """A verified webhook delivery, ready for rule dispatch."""

    kind: str
    source: str
    delivery_id: str
    event: dict[str, Any]
    received_at: int  # Unix milliseconds


@dataclass(frozen=True, slots=True)
class WebhookSessionRequest:
    """Request to create a session from a webhook rule."""

    workspace_path: str
    title: str
    prompt: str
    agent_preset: str
    permission_preset: str
    model: str | None = None


class WebhookRule(ABC):
    """Abstract base class for webhook rules."""

    def __init__(self, rule_id: str, kind: str):
        self.id = rule_id
        self.kind = kind

    @abstractmethod
    async def run(
        self,
        delivery: VerifiedWebhookDelivery,
        signal: asyncio.Event,
    ) -> WebhookSessionRequest | None:
        """Execute the rule.

        Args:
            delivery: The verified webhook delivery.
            signal: Cancellation signal.

        Returns:
            A session request, or None to not create a session.
        """
        ...


@dataclass
class RuleRegistration:
    """Internal rule registration with lifecycle tracking."""

    rule: WebhookRule
    controller: asyncio.Event
    active: set = field(default_factory=set)
    closing: bool = False


# ============================================================================
# Webhook Runtime
# ============================================================================


class WebhookRuntime:
    """Fire-and-forget webhook rule runtime."""

    def __init__(self):
        self._rules: dict[str, RuleRegistration] = {}
        self._closing = False

    def register(self, rule: WebhookRule) -> Callable[[], asyncio.coroutine]:
        """Register a webhook rule.

        Args:
            rule: The rule to register.

        Returns:
            An async disposer that aborts and drains the rule's active callbacks.
        """
        if self._closing:
            raise RuntimeError("webhook runtime is closing")
        if not rule.id or not rule.id.strip():
            raise ValueError("webhook rule id must be a non-empty string")
        if not rule.kind or not rule.kind.strip():
            raise ValueError(f'webhook rule "{rule.id}" kind must be a non-empty string')
        if not callable(rule.run):
            raise ValueError(f'webhook rule "{rule.id}" requires run()')

        if rule.id in self._rules:
            raise ValueError(f'webhook rule "{rule.id}" is already registered')

        controller = asyncio.Event()
        registration = RuleRegistration(
            rule=rule,
            controller=controller,
        )
        self._rules[rule.id] = registration

        async def dispose():
            await self._dispose_registration(registration)

        return dispose

    async def _dispose_registration(self, registration: RuleRegistration) -> None:
        registration.closing = True
        registration.controller.set()
        if registration.active:
            await asyncio.gather(*registration.active, return_exceptions=True)

    async def dispatch(
        self,
        kind: str,
        source: str,
        delivery_id: str,
        event: dict[str, Any],
        received_at: int,
    ) -> None:
        """Dispatch a verified delivery to all matching rules.

        This is fire-and-forget: it schedules each rule independently and returns
        before any callback settles.
        """
        delivery = VerifiedWebhookDelivery(
            kind=kind,
            source=source,
            delivery_id=delivery_id,
            event=event,
            received_at=received_at,
        )

        # Snapshot rules to avoid holding lock during async execution
        matching_rules = [
            reg for reg in self._rules.values() if reg.rule.kind == kind and not reg.closing
        ]

        for registration in matching_rules:
            if registration.closing:
                continue
            registration.controller.clear()
            task = asyncio.create_task(self._run_rule(registration, delivery))
            registration.active.add(task)
            task.add_done_callback(registration.active.discard)

    async def _run_rule(
        self,
        registration: RuleRegistration,
        delivery: VerifiedWebhookDelivery,
    ) -> None:
        """Run a single rule, handling errors and cancellation."""
        try:
            request = await registration.rule.run(delivery, registration.controller)
            if request is not None:
                await self._create_session(request, delivery)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Webhook rule %s failed: %s",
                registration.rule.id,
                e,
                exc_info=True,
            )
        finally:
            registration.controller.set()

    async def _create_session(
        self,
        request: WebhookSessionRequest,
        delivery: VerifiedWebhookDelivery,
    ) -> None:
        """Create a session from a webhook rule request."""
        # This would integrate with the chat service to create a conversation
        # For now, we log the request
        logger.info(
            "Webhook rule would create session: kind=%s source=%s title=%s",
            delivery.kind,
            delivery.source,
            request.title,
        )


# ============================================================================
# GitHub Webhook Handler
# ============================================================================


class GitHubWebhookHandler:
    """Signed GitHub webhook handler."""

    def __init__(
        self,
        runtime: WebhookRuntime,
        source: str,
        secret: str,
        max_body_bytes: int = 1_000_000,
    ):
        self.runtime = runtime
        self.source = source
        self.secret = secret.encode("utf-8")
        self.max_body_bytes = max_body_bytes

    async def handle(self, request: Request) -> Response:
        """Handle a GitHub webhook request."""
        # Read body with size limit
        body = await request.body()
        if len(body) > self.max_body_bytes:
            raise HTTPException(status_code=413, detail="Body too large")

        # Verify signature
        signature = request.headers.get("X-Hub-Signature-256")
        if not signature:
            raise HTTPException(status_code=401, detail="Missing signature")

        expected = "sha256=" + hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="Invalid signature")

        # Parse event
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        # Get delivery ID
        delivery_id = request.headers.get("X-GitHub-Delivery", str(uuid.uuid4()))

        # Dispatch to runtime
        received_at = int(datetime.now(UTC).timestamp() * 1000)
        await self.runtime.dispatch(
            kind="github",
            source=self.source,
            delivery_id=delivery_id,
            event=event,
            received_at=received_at,
        )

        return Response(status_code=202)


# ============================================================================
# Built-in Rules
# ============================================================================


class GitHubPushRule(WebhookRule):
    """Rule that triggers on GitHub push events."""

    def __init__(
        self,
        rule_id: str,
        *,
        branch_pattern: str = "main",
        preset: str = "standard",
        permission: str = "workspace-write",
    ):
        super().__init__(rule_id, "github")
        self.branch_pattern = branch_pattern
        self.preset = preset
        self.permission = permission

    async def run(
        self,
        delivery: VerifiedWebhookDelivery,
        signal: asyncio.Event,
    ) -> WebhookSessionRequest | None:
        event = delivery.event
        if event.get("ref") != f"refs/heads/{self.branch_pattern}":
            return None

        repo = event.get("repository", {})
        repo_name = repo.get("full_name", "unknown")
        pusher = event.get("pusher", {}).get("name", "unknown")
        commits = event.get("commits", [])

        if not commits:
            return None

        # Build prompt from commits
        commit_msgs = "\n".join(f"- {c.get('message', '').strip()}" for c in commits[:5])
        prompt = (
            f"Repository {repo_name} was pushed to branch {self.branch_pattern} by {pusher}.\n"
            f"Commits:\n{commit_msgs}\n\n"
            f"Please review the changes and run any relevant tests."
        )

        return WebhookSessionRequest(
            workspace_path=f"/workspace/{repo_name}",
            title=f"GitHub push: {repo_name}/{self.branch_pattern}",
            prompt=prompt,
            agent_preset=self.preset,
            permission_preset=self.permission,
        )


class GitHubPullRequestRule(WebhookRule):
    """Rule that triggers on GitHub pull request events."""

    def __init__(
        self,
        rule_id: str,
        *,
        action_patterns: list[str] = None,
        preset: str = "standard",
        permission: str = "workspace-write",
    ):
        super().__init__(rule_id, "github")
        self.action_patterns = action_patterns or ["opened", "synchronize", "reopened"]
        self.preset = preset
        self.permission = permission

    async def run(
        self,
        delivery: VerifiedWebhookDelivery,
        signal: asyncio.Event,
    ) -> WebhookSessionRequest | None:
        event = delivery.event
        action = event.get("action")
        if action not in self.action_patterns:
            return None

        pr = event.get("pull_request", {})
        repo = event.get("repository", {})
        repo_name = repo.get("full_name", "unknown")
        pr_number = pr.get("number")
        pr_title = pr.get("title", "")
        pr_body = pr.get("body", "")

        prompt = (
            f"Pull request #{pr_number} in {repo_name}: {action}\n"
            f"Title: {pr_title}\n"
            f"Body: {pr_body}\n\n"
            f"Please review the PR and run relevant checks."
        )

        return WebhookSessionRequest(
            workspace_path=f"/workspace/{repo_name}",
            title=f"GitHub PR #{pr_number}: {pr_title[:50]}",
            prompt=prompt,
            agent_preset=self.preset,
            permission_preset=self.permission,
        )


# ============================================================================
# Global Runtime & API
# ============================================================================

_webhook_runtime: WebhookRuntime | None = None


def get_webhook_runtime() -> WebhookRuntime:
    """Get the global webhook runtime."""
    global _webhook_runtime
    if _webhook_runtime is None:
        _webhook_runtime = WebhookRuntime()
    return _webhook_runtime


def create_github_webhook_router(
    settings: Settings,
    source: str = "github",
    path: str = "/webhooks/github",
    secret_env: str = "GITHUB_WEBHOOK_SECRET",
    max_body_bytes: int = 1_000_000,
) -> APIRouter:
    """Create a FastAPI router for GitHub webhooks."""
    import os

    secret = os.environ.get(secret_env)
    if not secret:
        raise RuntimeError(f"GitHub webhook secret not found in environment variable {secret_env}")

    runtime = get_webhook_runtime()
    handler = GitHubWebhookHandler(runtime, source, secret, max_body_bytes)

    router = APIRouter(prefix="", tags=["webhooks"])

    @router.post(path)
    async def github_webhook(request: Request):
        return await handler.handle(request)

    return router


def register_builtin_rules(runtime: WebhookRuntime) -> list[Callable]:
    """Register built-in GitHub rules.

    Returns:
        List of disposers for the registered rules.
    """
    disposers = []

    # Push to main branch
    push_rule = GitHubPushRule(
        rule_id="github-push-main",
        branch_pattern="main",
        preset="standard",
        permission="workspace-write",
    )
    disposers.append(runtime.register(push_rule))

    # Pull request opened/synchronized
    pr_rule = GitHubPullRequestRule(
        rule_id="github-pr-opened",
        action_patterns=["opened", "synchronize", "reopened"],
        preset="standard",
        permission="workspace-write",
    )
    disposers.append(runtime.register(pr_rule))

    return disposers

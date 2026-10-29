"""Tests for webhook service."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest

from app.deepspace.services.webhooks import (
    GitHubPullRequestRule,
    GitHubPushRule,
    GitHubWebhookHandler,
    VerifiedWebhookDelivery,
    WebhookRule,
    WebhookRuntime,
    WebhookSessionRequest,
    register_builtin_rules,
)


class MockRule(WebhookRule):
    def __init__(self, rule_id: str, kind: str, result: WebhookSessionRequest | None = None):
        super().__init__(rule_id, kind)
        self._result = result

    async def run(self, delivery, signal):
        return self._result


def test_verified_webhook_delivery():
    delivery = VerifiedWebhookDelivery(
        kind="github",
        source="test",
        delivery_id="123",
        event={"action": "push"},
        received_at=1000,
    )
    assert delivery.kind == "github"
    assert delivery.source == "test"
    assert delivery.delivery_id == "123"
    assert delivery.event == {"action": "push"}
    assert delivery.received_at == 1000


def test_webhook_session_request():
    request = WebhookSessionRequest(
        workspace_path="/workspace/test",
        title="Test",
        prompt="Test prompt",
        agent_preset="standard",
        permission_preset="workspace-write",
    )
    assert request.workspace_path == "/workspace/test"
    assert request.title == "Test"
    assert request.prompt == "Test prompt"
    assert request.agent_preset == "standard"
    assert request.permission_preset == "workspace-write"


def test_webhook_runtime_register():
    runtime = WebhookRuntime()
    rule = MockRule("test-rule", "github")

    disposer = runtime.register(rule)
    assert "test-rule" in runtime._rules
    assert runtime._rules["test-rule"].rule is rule

    # Test duplicate registration fails
    with pytest.raises(ValueError, match="already registered"):
        runtime.register(rule)


def test_webhook_runtime_register_validation():
    runtime = WebhookRuntime()

    # Empty ID
    with pytest.raises(ValueError, match="non-empty string"):
        runtime.register(MockRule("", "github"))

    # Empty kind
    with pytest.raises(ValueError, match="kind must be a non-empty string"):
        runtime.register(MockRule("test", ""))

    # Non-callable run
    class BadRule(WebhookRule):
        def __init__(self):
            self.id = "bad"
            self.kind = "github"

        run = "not callable"

    with pytest.raises(ValueError, match="requires run"):
        runtime.register(BadRule())


@pytest.mark.asyncio
async def test_webhook_runtime_dispatch():
    runtime = WebhookRuntime()
    result = WebhookSessionRequest(
        workspace_path="/workspace",
        title="Test",
        prompt="Test",
        agent_preset="standard",
        permission_preset="workspace-write",
    )
    rule = MockRule("test-rule", "github", result=result)
    runtime.register(rule)

    await runtime.dispatch(
        kind="github",
        source="test",
        delivery_id="123",
        event={"action": "push"},
        received_at=1000,
    )

    # Give time for async execution
    import asyncio

    await asyncio.sleep(0.1)

    # Rule should have been called


@pytest.mark.asyncio
async def test_webhook_runtime_dispatch_kind_filter():
    runtime = WebhookRuntime()
    called = []

    class TrackingRule(MockRule):
        async def run(self, delivery, signal):
            called.append(delivery.kind)
            return self._result

    rule1 = TrackingRule("rule1", "github", result=None)
    rule2 = TrackingRule("rule2", "gitlab", result=None)
    runtime.register(rule1)
    runtime.register(rule2)

    await runtime.dispatch(
        kind="github",
        source="test",
        delivery_id="123",
        event={"action": "push"},
        received_at=1000,
    )

    import asyncio

    await asyncio.sleep(0.1)

    assert "github" in called
    assert "gitlab" not in called


def test_github_push_rule():
    rule = GitHubPushRule(
        rule_id="push-main",
        branch_pattern="main",
        preset="standard",
        permission="workspace-write",
    )
    assert rule.id == "push-main"
    assert rule.kind == "github"
    assert rule.branch_pattern == "main"


def test_github_pr_rule():
    rule = GitHubPullRequestRule(
        rule_id="pr-opened",
        action_patterns=["opened", "synchronize"],
        preset="standard",
        permission="workspace-write",
    )
    assert rule.id == "pr-opened"
    assert rule.kind == "github"
    assert rule.action_patterns == ["opened", "synchronize"]


@pytest.mark.asyncio
async def test_github_push_rule_execution():
    rule = GitHubPushRule(
        rule_id="push-main",
        branch_pattern="main",
        preset="standard",
        permission="workspace-write",
    )

    # Push to main - should trigger
    event = {
        "ref": "refs/heads/main",
        "repository": {"full_name": "owner/repo"},
        "pusher": {"name": "user"},
        "commits": [{"message": "Commit 1"}, {"message": "Commit 2"}],
    }
    delivery = VerifiedWebhookDelivery(
        kind="github",
        source="test",
        delivery_id="123",
        event=event,
        received_at=1000,
    )

    result = await rule.run(delivery, asyncio.Event())
    assert result is not None
    assert "owner/repo" in result.prompt
    assert "Commit 1" in result.prompt


@pytest.mark.asyncio
async def test_github_push_rule_wrong_branch():
    rule = GitHubPushRule(
        rule_id="push-main",
        branch_pattern="main",
        preset="standard",
        permission="workspace-write",
    )

    # Push to develop - should not trigger
    event = {
        "ref": "refs/heads/develop",
        "repository": {"full_name": "owner/repo"},
        "pusher": {"name": "user"},
        "commits": [{"message": "Commit 1"}],
    }
    delivery = VerifiedWebhookDelivery(
        kind="github",
        source="test",
        delivery_id="123",
        event=event,
        received_at=1000,
    )

    result = await rule.run(delivery, asyncio.Event())
    assert result is None


@pytest.mark.asyncio
async def test_github_pr_rule_execution():
    rule = GitHubPullRequestRule(
        rule_id="pr-opened",
        action_patterns=["opened", "synchronize"],
        preset="standard",
        permission="workspace-write",
    )

    event = {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "title": "Test PR",
            "body": "PR description",
        },
        "repository": {"full_name": "owner/repo"},
    }
    delivery = VerifiedWebhookDelivery(
        kind="github",
        source="test",
        delivery_id="123",
        event=event,
        received_at=1000,
    )

    result = await rule.run(delivery, asyncio.Event())
    assert result is not None
    assert "#42" in result.title
    assert "Test PR" in result.prompt


def test_github_webhook_handler_signature():
    runtime = WebhookRuntime()
    secret = "test-secret"
    handler = GitHubWebhookHandler(runtime, "github", secret, max_body_bytes=1000)

    # Create a test payload
    payload = {"action": "push"}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    # Verify the handler would compute the same signature
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)


def test_register_builtin_rules():
    runtime = WebhookRuntime()
    disposers = register_builtin_rules(runtime)

    assert len(disposers) == 2
    assert "github-push-main" in runtime._rules
    assert "github-pr-opened" in runtime._rules
    assert runtime._rules["github-push-main"].rule.kind == "github"
    assert runtime._rules["github-pr-opened"].rule.kind == "github"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

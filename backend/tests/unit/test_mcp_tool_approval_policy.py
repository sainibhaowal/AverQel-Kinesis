from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.integrations.api.mcp import (
    _configure_composio_authorized_policy,
    _policy_defaults,
)
from app.integrations.services.mcp_runtime import (
    evaluate_mcp_tool_policy,
    infer_mcp_tool_risk,
)


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _Db:
    def __init__(self, policy: object) -> None:
        self.policy = policy

    def execute(self, _statement: object) -> _Result:
        return _Result(self.policy)


def _policy(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "tenant_id": uuid4(),
        "user_id": uuid4(),
        "server_id": uuid4(),
        "allowed_tools": [],
        "denied_tools": [],
        "read_only": True,
        "risk_ceiling": "read",
        "approval_rules": {
            "write": "needs_approval",
            "delete": "needs_approval",
            "external_message": "needs_approval",
        },
        "tool_modes": {},
        "default_tool_mode": "always_allow",
        "default_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _evaluate(policy: SimpleNamespace, name: str, *, labels: list[str] | None = None):
    server = SimpleNamespace(
        id=policy.server_id,
        tenant_id=policy.tenant_id,
        user_id=policy.user_id,
        registry_entry_id=None,
        enabled=True,
        status="connected",
    )
    return evaluate_mcp_tool_policy(
        db=_Db(policy),  # type: ignore[arg-type]
        server=server,  # type: ignore[arg-type]
        tool_name=name,
        tenant_id=policy.tenant_id,
        user_id=policy.user_id,
        conversation_id=uuid4(),
        tool={"name": name, "risk_labels": labels or []},
    )


def test_read_only_connection_runs_routine_reads_without_reapproval() -> None:
    decision = _evaluate(_policy(), "search_files", labels=["read"])
    command_runner = _evaluate(_policy(), "run_shell_command", labels=["read"])

    assert decision.allowed is True
    assert decision.approval_requirement == "auto"
    assert decision.requires_approval is False
    assert command_runner.allowed is True
    assert command_runner.requires_approval is True


def test_explicitly_allowed_routine_write_runs_without_reapproval() -> None:
    policy = _policy(
        read_only=False,
        risk_ceiling="write",
        approval_rules={"write": "always_allow"},
        default_tool_mode="needs_approval",
    )

    decision = _evaluate(policy, "update_document", labels=["write"])

    assert decision.allowed is True
    assert decision.risk_level == "write"
    assert decision.approval_requirement == "auto"


def test_risk_level_needs_approval_overrides_account_wide_auto_mode() -> None:
    policy = _policy(
        read_only=False,
        risk_ceiling="write",
        approval_rules={"write": "needs_approval"},
        default_tool_mode="always_allow",
        tool_modes={"create_document": "always_allow"},
    )

    decision = _evaluate(policy, "create_document", labels=["write"])

    assert decision.allowed is True
    assert decision.requires_approval is True


def test_delete_and_external_message_always_need_human_review() -> None:
    policy = _policy(
        read_only=False,
        risk_ceiling="external_message",
        approval_rules={
            "delete": "always_allow",
            "external_message": "always_allow",
        },
        tool_modes={"delete_document": "always_allow", "send_message": "always_allow"},
    )

    for name, risk in (("delete_document", "delete"), ("send_message", "external_message")):
        decision = _evaluate(policy, name, labels=[risk])
        assert decision.allowed is True
        assert decision.risk_level == risk
        assert decision.requires_approval is True


def test_sensitive_and_unclassified_writes_do_not_auto_run() -> None:
    policy = _policy(
        read_only=False,
        risk_ceiling="write",
        approval_rules={"write": "always_allow"},
    )

    sensitive = _evaluate(policy, "update_account_permissions", labels=["write"])
    assert sensitive.allowed is True
    assert sensitive.requires_approval is True

    command_runner = _evaluate(policy, "execute_command", labels=["write"])
    assert command_runner.allowed is True
    assert command_runner.requires_approval is True

    unclassified = _evaluate(policy, "perform_custom_action")
    assert unclassified.risk_level == "write"
    assert unclassified.requires_approval is True


def test_name_based_danger_overrides_a_lower_catalog_label() -> None:
    assert infer_mcp_tool_risk("delete_all_records", {"risk_labels": ["read"]}) == "delete"


def test_new_connection_defaults_and_composio_consent_policy() -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    server = SimpleNamespace(tenant_id=tenant_id, user_id=user_id, id=uuid4())

    standard = _policy_defaults(server)  # type: ignore[arg-type]
    assert standard.read_only is True
    assert standard.risk_ceiling == "read"
    assert standard.default_tool_mode == "always_allow"

    composio = _policy_defaults(server)  # type: ignore[arg-type]
    _configure_composio_authorized_policy(composio)
    assert composio.read_only is False
    assert composio.risk_ceiling == "external_message"
    assert composio.default_tool_mode == "always_allow"
    assert composio.approval_rules == {
        "write": "always_allow",
        "delete": "needs_approval",
        "external_message": "needs_approval",
    }

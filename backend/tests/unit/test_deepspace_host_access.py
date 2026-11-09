from __future__ import annotations

from pathlib import Path

from app.deepspace.services.host_access import (
    blocked_shell_command_reason,
    shell_command_needs_confirmation,
    tool_requires_human_confirmation,
)
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy


def test_routine_shell_commands_do_not_need_confirmation() -> None:
    assert shell_command_needs_confirmation("pytest -q") is False
    assert shell_command_needs_confirmation("python -m compileall app") is False
    assert shell_command_needs_confirmation("ls -la && git status --short") is False
    assert shell_command_needs_confirmation("systemctl status example.service") is False
    assert shell_command_needs_confirmation("defaults read com.example.app") is False
    assert shell_command_needs_confirmation("gsettings get org.example key") is False


def test_shell_delete_and_move_paths_outside_the_workspace_need_confirmation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert (
        shell_command_needs_confirmation(
            "rm output.log",
            permission_mode="danger-full-access",
            workspace_root=workspace,
        )
        is False
    )
    assert (
        shell_command_needs_confirmation(
            "rm /tmp/outside.log",
            permission_mode="danger-full-access",
            workspace_root=workspace,
        )
        is True
    )
    assert (
        shell_command_needs_confirmation(
            "mv src/output.log ~/Downloads/output.log",
            permission_mode="danger-full-access",
            workspace_root=workspace,
        )
        is True
    )


def test_broad_deletion_and_system_changes_need_confirmation() -> None:
    assert shell_command_needs_confirmation("rm -rf build/generated") is True
    assert shell_command_needs_confirmation("rmdir /s /q old-folder") is True
    assert shell_command_needs_confirmation("git reset --hard") is True
    assert shell_command_needs_confirmation("git checkout -- .") is True
    assert shell_command_needs_confirmation("shutdown now") is True
    assert shell_command_needs_confirmation("systemctl restart example.service") is True
    assert (
        shell_command_needs_confirmation("defaults write com.example.app Enabled -bool YES") is True
    )
    assert shell_command_needs_confirmation("gsettings set org.example key value") is True
    assert shell_command_needs_confirmation("bash -lc 'rm -rf build'") is True
    assert shell_command_needs_confirmation("env SAFE=1 rm -rf build") is True


def test_privilege_escalation_and_machine_wipe_commands_remain_blocked() -> None:
    assert blocked_shell_command_reason("sudo apt install example")
    assert blocked_shell_command_reason("dd if=/dev/zero of=/dev/sda")
    assert blocked_shell_command_reason("dd if=/dev/zero of=/dev/mapper/root")
    assert blocked_shell_command_reason("rm -rf /")


def test_sensitive_host_paths_need_confirmation_but_ordinary_host_paths_do_not(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert (
        tool_requires_human_confirmation(
            "fs_write",
            {"path": str(tmp_path / "output.csv")},
            permission_mode="danger-full-access",
            workspace_root=workspace,
        )
        is False
    )
    assert (
        tool_requires_human_confirmation(
            "fs_write",
            {"path": "/etc/example.conf"},
            permission_mode="danger-full-access",
            workspace_root=workspace,
        )
        is True
    )
    assert (
        tool_requires_human_confirmation(
            "fs_write",
            {"path": ".git/config"},
            permission_mode="danger-full-access",
            workspace_root=workspace,
        )
        is True
    )


def test_powershell_file_writes_only_prompt_for_protected_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert (
        shell_command_needs_confirmation(
            "Set-Content -Path note.md -Value hello",
            permission_mode="danger-full-access",
            workspace_root=workspace,
        )
        is False
    )
    assert (
        shell_command_needs_confirmation(
            "Set-Content -Path /etc/example.conf -Value hello",
            permission_mode="danger-full-access",
            workspace_root=workspace,
        )
        is True
    )


def test_computer_actions_are_not_blanket_prompted_but_known_risky_inputs_are() -> None:
    assert (
        tool_requires_human_confirmation(
            "computer_click",
            {"x": 25, "y": 80},
            permission_mode="danger-full-access",
        )
        is False
    )
    assert (
        tool_requires_human_confirmation(
            "computer_type",
            {"text": "rm -rf /"},
            permission_mode="danger-full-access",
        )
        is True
    )
    assert (
        tool_requires_human_confirmation(
            "computer_hotkey",
            {"keys": ["ctrl", "alt", "delete"]},
            permission_mode="danger-full-access",
        )
        is True
    )


def test_browser_actions_are_gated_by_risk_cues_in_the_target() -> None:
    assert (
        tool_requires_human_confirmation(
            "browser_click",
            {"selector": "button#continue"},
            permission_mode="danger-full-access",
        )
        is False
    )
    assert (
        tool_requires_human_confirmation(
            "browser_click",
            {"selector": "button#delete-account"},
            permission_mode="danger-full-access",
        )
        is True
    )
    assert (
        tool_requires_human_confirmation(
            "browser_evaluate",
            {"script": "document.title"},
            permission_mode="danger-full-access",
        )
        is True
    )


def test_full_access_does_not_prompt_for_read_only_plugin_calls() -> None:
    policy = DeepSpaceToolPolicy(
        permission_mode="danger-full-access",
        plugin_modes={"extension_lookup": "read", "extension_update": "write"},
    )

    read = policy.decide("extension_lookup", {})
    write = policy.decide("extension_update", {})

    assert read.allowed is True
    assert read.requires_approval is False
    assert write.allowed is True
    assert write.requires_approval is True

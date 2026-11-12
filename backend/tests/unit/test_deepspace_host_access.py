from __future__ import annotations

from pathlib import Path

from app.deepspace.services.host_access import (
    blocked_shell_command_reason,
    find_mutation_has_relative_root,
    is_known_read_only_command,
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


def test_find_file_search_and_stderr_to_dev_null_are_safe_reads() -> None:
    command = "find /home -type f -name '*.csv' 2>/dev/null"
    exact_filename_search = (
        "find /home/AverQel -name 'Q4-FY23-Revenue-and-Earnings.pdf' 2>/dev/null"
    )

    assert is_known_read_only_command(command) is True
    assert shell_command_needs_confirmation(command, permission_mode="danger-full-access") is False
    assert is_known_read_only_command(exact_filename_search) is True
    assert (
        shell_command_needs_confirmation(
            exact_filename_search, permission_mode="danger-full-access"
        )
        is False
    )

    policy = DeepSpaceToolPolicy(
        approval_mode="ask",
        permission_mode="danger-full-access",
    )
    decision = policy.decide("bash", {"command": command})

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_read_only_ls_with_permission_skips_does_not_need_approval() -> None:
    command = 'ls -la /home/AverQel 2>/dev/null || echo "Directory not accessible"'

    assert shell_command_needs_confirmation(command, permission_mode="danger-full-access") is False
    decision = DeepSpaceToolPolicy(
        approval_mode="ask", permission_mode="danger-full-access"
    ).decide("bash", {"command": command})
    assert decision.requires_approval is False


def test_find_mutating_actions_still_need_review_or_are_blocked() -> None:
    assert is_known_read_only_command("find /home -type f -name '*.csv'") is True
    assert is_known_read_only_command("find /home -type f -delete") is False
    assert shell_command_needs_confirmation("find /home -type f -delete") is True
    assert shell_command_needs_confirmation("find /home -exec rm {} +") is True

    assert blocked_shell_command_reason("find / -delete")
    assert blocked_shell_command_reason("bash -lc 'find /etc -delete'")
    assert blocked_shell_command_reason("find /System -delete")
    assert blocked_shell_command_reason("find /media/backup -delete")
    assert blocked_shell_command_reason("cd / && find . -delete")
    assert blocked_shell_command_reason("find . -delete", cwd="/")


def test_persistent_terminal_does_not_run_relative_find_mutations() -> None:
    policy = DeepSpaceToolPolicy(permission_mode="danger-full-access")
    decision = policy.decide("terminal_write", {"data": "find . -delete"})

    assert find_mutation_has_relative_root("find . -delete") is True
    assert find_mutation_has_relative_root("find /home -delete") is False
    assert decision.allowed is False


def test_only_exact_stderr_redirection_is_allowlisted_for_safe_reads() -> None:
    safe_read = "nvidia-smi 2>/dev/null"
    stdout_redirect = "find /home -type f -name '*.csv' >/dev/null"

    assert is_known_read_only_command(safe_read) is True
    assert shell_command_needs_confirmation(safe_read) is False
    assert is_known_read_only_command(stdout_redirect) is False
    assert shell_command_needs_confirmation(stdout_redirect) is True


def test_file_deletes_and_moves_need_confirmation(
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
        is True
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
    assert shell_command_needs_confirmation("mv src/old.py src/new.py") is True
    assert tool_requires_human_confirmation(
        "edit", {"target": "library", "operation": "move", "file_id": "file-1"}
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


def test_disk_wipe_and_security_bypass_commands_are_blocked() -> None:
    blocked_commands = (
        "shred -n 3 /dev/sda",
        "blkdiscard /dev/nvme0n1",
        "cat /dev/zero > /dev/sda",
        "truncate -s 0 /dev/sda",
        "fdisk /dev/sda",
        "sfdisk /dev/sda",
        "sgdisk --zap-all /dev/sda",
        "parted /dev/sda mklabel gpt",
        "cryptsetup luksFormat /dev/sda",
        "diskutil eraseDisk APFS Data /dev/disk0",
        "Clear-Disk -Number 0 -RemoveData",
        "setenforce 0",
        "ufw disable",
        "iptables -F",
        "nft flush ruleset",
        "systemctl disable firewalld",
        "spctl --master-disable",
        "netsh advfirewall set allprofiles state off",
        r"reg add HKLM\SOFTWARE\Policies\Microsoft\Windows Defender /v DisableRealtimeMonitoring /d 1",
        r"reg add HKLM\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\DomainProfile /v EnableFirewall /d 0",
        r"reg add HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System /v EnableLUA /d 0",
        "sc stop WinDefend",
        "Set-MpPreference -DisableRealtimeMonitoring $true",
        "Set-ExecutionPolicy Bypass",
    )

    for command in blocked_commands:
        assert blocked_shell_command_reason(command), command
    assert blocked_shell_command_reason("dd if=/dev/zero of=/tmp/disk.img") is None
    assert shell_command_needs_confirmation("dd if=/dev/zero of=/tmp/disk.img") is True


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

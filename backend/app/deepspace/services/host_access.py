"""Path and command safeguards for conversation-scoped host access."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

from app.deepspace.services.workspace_root import (
    MAX_PATH_LENGTH,
    ResolvedWorkspacePath,
    WorkspacePathError,
    resolve_within_root,
)


def resolve_conversation_path(
    workspace_root: Path,
    raw_path: str,
    mode: str | None,
) -> ResolvedWorkspacePath:
    """Resolve a file path according to the conversation's effective mode.

    Workspace modes remain rooted in the selected workspace. Full access may
    name any path the backend process's operating-system user can access.
    Relative paths in Full access remain relative to the selected workspace.
    """
    if str(mode or "").strip().lower() != "danger-full-access":
        return resolve_within_root(workspace_root, raw_path)

    text = str(raw_path or "").strip()
    if not text:
        raise WorkspacePathError("A path is required.")
    if len(text) > MAX_PATH_LENGTH:
        raise WorkspacePathError(f"A path may not exceed {MAX_PATH_LENGTH} characters.")
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = workspace_root / path
        absolute = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspacePathError("The requested host path could not be resolved.") from exc
    return ResolvedWorkspacePath(absolute=absolute, relative=str(absolute))


def protected_host_path_reason(path: Path) -> str | None:
    """Return a warning for core operating-system paths that require review."""
    try:
        candidate = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return "The host path could not be safely resolved."

    if os.name == "nt":
        windows_roots = [
            os.environ.get("WINDIR"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("PROGRAMDATA"),
            os.environ.get("APPDATA"),
            os.environ.get("LOCALAPPDATA"),
        ]
        normalized = os.path.normcase(str(candidate))
        for root in windows_roots:
            if not root:
                continue
            windows_protected_path = os.path.normcase(str(Path(root).resolve(strict=False)))
            if normalized == windows_protected_path or normalized.startswith(
                windows_protected_path + os.sep
            ):
                return (
                    "This path is part of Windows or installed software and needs explicit review."
                )
        return None

    roots = (
        "/boot",
        "/dev",
        "/etc",
        "/lib",
        "/lib32",
        "/lib64",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/sys",
        "/usr",
        "/usr/local",
        "/var/lib",
        "/var/log",
        "/opt",
        "/snap",
        "/System",
        "/Library",
        "/Applications",
        "/private/etc",
        "/private/var",
    )
    user_settings_roots = (
        Path.home() / ".config",
        Path.home() / ".ssh",
        Path.home() / ".gnupg",
        Path.home() / "Library" / "LaunchAgents",
        Path.home() / "Library" / "Preferences",
        Path.home() / "Library" / "Keychains",
    )
    protected_roots = tuple(Path(root) for root in roots) + user_settings_roots
    for protected_root in protected_roots:
        try:
            candidate.relative_to(protected_root)
        except ValueError:
            continue
        return "This path is part of the operating system or installed software and needs explicit review."
    return None


# These operations have a meaningful effect outside a routine file edit. The
# checks below intentionally leave ordinary build, test, and file-editing
# commands alone while asking about broad deletion, system administration, or
# other high-impact actions.
_HIGH_IMPACT_SHELL_COMMANDS = frozenset(
    {
        "apt",
        "apt-get",
        "chown",
        "chmod",
        "crontab",
        "dconf",
        "defaults",
        "diskpart",
        "doas",
        "format",
        "gksu",
        "gsettings",
        "halt",
        "kdesu",
        "launchctl",
        "loginctl",
        "mkfs",
        "mkfs.ext4",
        "mkfs.xfs",
        "mount",
        "netsh",
        "new-itemproperty",
        "nvram",
        "pkexec",
        "powercfg",
        "poweroff",
        "reboot",
        "reg",
        "reg.exe",
        "remove-itemproperty",
        "restart-computer",
        "runas",
        "runuser",
        "schtasks",
        "service",
        "set-executionpolicy",
        "set-itemproperty",
        "setpriv",
        "shred",
        "shutdown",
        "stop-computer",
        "su",
        "sudo",
        "sysctl",
        "systemctl",
        "tee",
        "umount",
        "wipefs",
    }
)
_HIGH_IMPACT_TERMS = re.compile(
    r"\b(?:delete|remove|erase|wipe|format|reset|purchase|pay|checkout|send|"
    r"submit|publish|post|transfer|approve|grant|revoke|unsubscribe|cancel|"
    r"install|uninstall|password|payment|billing|security|permissions?)\b",
    re.I,
)
_INLINE_DESTRUCTIVE_CODE = re.compile(
    r"\b(?:rmtree|rm_rf|rm_r|rmsync|unlink(?:sync)?|rmdir(?:sync)?|remove(?:sync)?|"
    r"rm\s+-[a-z]*r[a-z]*|wipefs|mkfs|shutdown|poweroff|"
    r"reboot|systemctl\s+(?:stop|disable|mask|edit|set-property))\b",
    re.I,
)
_NEVER_RUN_PATTERNS = (
    re.compile(r"\brm\s+(?:-[^\s]*\s+)*-[^\s]*r[^\s]*f[^\s]*\s+/(?:\s|$|\*)", re.I),
    re.compile(
        r"\brm\s+(?:-[^\s]*\s+)*(?:--recursive\s+.*?--force|--force\s+.*?--recursive)\s+"
        r"(?:/etc|/usr|/boot|/dev|/proc|/root|/run|/sbin|/sys|/var/lib|/var/log)(?:/|\s|$)",
        re.I,
    ),
    re.compile(r"\bdd\b[^\n]*\bof=/dev/(?!null(?:\s|$))", re.I),
    re.compile(r"\b(?:mkfs(?:\.\w+)?|wipefs)\b[^\n]*/dev/(?!null(?:\s|$))", re.I),
    re.compile(r"\bdiskpart\b", re.I),
    re.compile(r"\b(?:sudo|doas|runas|su|pkexec|runuser|setpriv|gksu|kdesu)\b", re.I),
    re.compile(r"\bformat\s+(?:/[a-z]+\s+)*[a-z]:", re.I),
    re.compile(
        r"\b(?:remove-item|del|erase)\s+(?:-[a-z]+\s+)*(?:c:\\windows|%windir%|"
        r"/etc|/usr|/boot|/dev|/proc|/root|/run|/sbin|/sys|/var/lib|/var/log)(?:\\|/|\s|$)",
        re.I,
    ),
)
_READ_ONLY_COMMANDS = frozenset(
    {
        "cat",
        "cut",
        "date",
        "df",
        "du",
        "file",
        "head",
        "id",
        "ls",
        "printf",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "stat",
        "tail",
        "uname",
        "wc",
        "which",
        "whoami",
        # Common PowerShell inspection cmdlets. Writes remain blocked by the
        # read-only OS cage even when a shell expression is not recognized.
        "get-childitem",
        "get-command",
        "get-content",
        "get-date",
        "get-help",
        "get-item",
        "get-location",
        "get-process",
        "get-service",
        "measure-object",
        "out-string",
        "resolve-path",
        "select-string",
        "sort-object",
        "test-path",
        "where-object",
    }
)


def blocked_shell_command_reason(command: str) -> str | None:
    """Block host-level commands that could destroy or elevate the system."""
    text = str(command or "")
    if any(pattern.search(text) for pattern in _NEVER_RUN_PATTERNS):
        return (
            "DeepSpace blocked this command because it can damage the operating system, "
            "erase a disk, modify a protected operating-system path, or elevate privileges."
        )
    return None


def shell_command_needs_confirmation(
    command: str,
    *,
    permission_mode: str | None = None,
    workspace_root: Path | None = None,
    cwd: str | None = None,
    _depth: int = 0,
) -> bool:
    """Identify high-impact shell actions without prompting for routine commands."""
    text = str(command or "")
    if blocked_shell_command_reason(text):
        return False
    if _depth > 2:
        return True
    # Preserve command boundaries in multi-line shell input. Newlines inside a
    # quoted command remain quoted after lexing and are not treated as shell
    # separators.
    text = text.replace("\r\n", ";").replace("\n", ";")
    try:
        lexer = shlex.shlex(text, posix=os.name != "nt", punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # Unparseable shell syntax is treated cautiously in Full access.
        return True
    if not tokens:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {";", "&&", "||", "|", "&", "\n"}:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        if not segment:
            continue
        words = [Path(token.strip("\"'")).name.lower() for token in segment]
        # Unwrap common command launchers and leading environment assignments
        # so a safety-sensitive command cannot avoid classification by being
        # prefixed with `env`, `time`, or `VAR=value`.
        while words and (
            words[0] in {"env", "command", "exec", "nohup", "time", "nice", "stdbuf", "setsid"}
            or re.fullmatch(r"[a-z_][a-z0-9_]*=.*", words[0], re.I)
        ):
            segment = segment[1:]
            words = words[1:]
        if not words:
            continue
        if (
            words[0] in {"systemctl", "service"}
            and len(words) > 1
            and words[1]
            in {"status", "is-active", "is-enabled", "show", "list-units", "list-unit-files", "cat"}
        ):
            continue
        if (
            words[0] == "loginctl"
            and len(words) > 1
            and words[1] in {"list-sessions", "show-session", "seat-status", "user-status"}
        ):
            continue
        if (
            words[0] in {"apt", "apt-get"}
            and len(words) > 1
            and words[1] in {"list", "search", "show", "policy", "depends", "rdepends"}
        ):
            continue
        if words[0] == "mount" and (
            len(words) == 1 or "-l" in words[1:] or "--show-labels" in words[1:]
        ):
            continue
        if words[0] == "powercfg" and any(
            token.lower() in {"/query", "/list", "/getactivescheme"} for token in words[1:]
        ):
            continue
        if (
            words[0] == "sysctl"
            and any(token in {"-a", "--all", "-n"} for token in words[1:])
            and not any("=" in token for token in words[1:])
        ):
            continue
        if words[0] == "defaults" and len(words) > 1 and words[1] in {"read", "domains"}:
            continue
        if (
            words[0] == "gsettings"
            and len(words) > 1
            and words[1]
            in {"get", "list-schemas", "list-recursively", "list-keys", "range", "describe"}
        ):
            continue
        if words[0] == "dconf" and len(words) > 1 and words[1] in {"read", "list"}:
            continue
        if words[0] in {"reg", "reg.exe"} and len(words) > 1 and words[1] == "query":
            continue
        if words[0] == "launchctl" and len(words) > 1 and words[1] in {"list", "print", "procinfo"}:
            continue
        if words[0] == "netsh" and "show" in words[1:]:
            continue
        if words[0] == "schtasks" and "/query" in words[1:]:
            continue
        if words[0] == "crontab" and "-l" in words[1:]:
            continue
        if words[0] in _HIGH_IMPACT_SHELL_COMMANDS or words[0].startswith("mkfs."):
            return True
        if words[0] in {"bash", "sh", "zsh", "pwsh", "powershell", "cmd"}:
            command_flags = {"-c", "-command", "/c"}
            for index, token in enumerate(words[1:], start=1):
                is_shell_c_flag = (
                    words[0] in {"bash", "sh", "zsh"} and token.startswith("-") and "c" in token[1:]
                )
                if token in command_flags or is_shell_c_flag:
                    nested = " ".join(segment[index + 1 :])
                    if shell_command_needs_confirmation(
                        nested,
                        permission_mode=permission_mode,
                        workspace_root=workspace_root,
                        cwd=cwd,
                        _depth=_depth + 1,
                    ):
                        return True
                    break
        if words[0] in {"rm", "rmdir", "unlink", "shred"}:
            flags = {token.lower() for token in segment[1:] if token.startswith("-")}
            operands = [token for token in segment[1:] if not token.startswith("-")]
            broad_target = any(
                token in {"/", ".", "..", "*", "**", "~"}
                or "*" in token
                or ".git" in {part.lower() for part in Path(token).parts}
                for token in operands
            )
            recursive = (
                "--recursive" in flags
                or any("r" in flag.lstrip("-") for flag in flags if not flag.startswith("--"))
                or any(token.lower() in {"/s", "/q", "-p"} for token in segment[1:])
            )
            force_recursive = "--recursive" in flags and "--force" in flags
            if words[0] == "shred" or recursive or force_recursive or broad_target:
                return True
            if permission_mode == "danger-full-access" and workspace_root is None:
                return True
            if permission_mode == "danger-full-access" and workspace_root is not None:
                base = workspace_root
                if cwd:
                    requested_cwd = Path(cwd).expanduser()
                    base = (
                        requested_cwd
                        if requested_cwd.is_absolute()
                        else workspace_root / requested_cwd
                    )
                for operand in operands:
                    target = Path(operand).expanduser()
                    if not target.is_absolute():
                        target = base / target
                    if _path_requires_confirmation(
                        str(target),
                        permission_mode=permission_mode,
                        workspace_root=workspace_root,
                        outside_requires_confirmation=True,
                    ):
                        return True
        if words[0] in {"mv", "move-item", "rename-item"}:
            operands = [
                token for token in segment[1:] if not token.startswith("-") and token != "--"
            ]
            if permission_mode == "danger-full-access" and workspace_root is None:
                return True
            if permission_mode == "danger-full-access" and workspace_root is not None:
                base = workspace_root
                if cwd:
                    requested_cwd = Path(cwd).expanduser()
                    base = (
                        requested_cwd
                        if requested_cwd.is_absolute()
                        else workspace_root / requested_cwd
                    )
                for operand in operands:
                    target = Path(operand).expanduser()
                    if not target.is_absolute():
                        target = base / target
                    if _path_requires_confirmation(
                        str(target),
                        permission_mode=permission_mode,
                        workspace_root=workspace_root,
                        outside_requires_confirmation=True,
                    ):
                        return True
        if words[0] == "git" and any(
            token.lower()
            in {
                "apply",
                "checkout",
                "cherry-pick",
                "clean",
                "commit",
                "merge",
                "pull",
                "push",
                "rebase",
                "reset",
                "restore",
                "revert",
                "stash",
            }
            for token in words[1:]
        ):
            return True
        if words[0] == "git" and len(words) > 1:
            subcommand = words[1]
            git_args = {token.lower() for token in words[2:]}
            if subcommand == "branch" and bool(
                git_args & {"-c", "-d", "-m", "--copy", "--delete", "--move"}
            ):
                return True
            if subcommand == "tag" and "-d" in git_args:
                return True
            if subcommand == "config" and bool(git_args & {"--global", "--system"}):
                return True
        if words[0] == "find" and any(token in {"-delete", "-exec", "-execdir"} for token in words):
            return True
        if words[0] in {"remove-item", "del", "erase"}:
            if any(token.lower() in {"-recurse", "-force", "/s", "/q"} for token in segment[1:]):
                return True
            if permission_mode == "danger-full-access" and workspace_root is None:
                return True
            if permission_mode == "danger-full-access" and workspace_root is not None:
                operands = [
                    token for token in segment[1:] if not token.startswith("-") and token != "--"
                ]
                for operand in operands:
                    target = Path(operand).expanduser()
                    if not target.is_absolute():
                        target = workspace_root / target
                    if _path_requires_confirmation(
                        str(target),
                        permission_mode=permission_mode,
                        workspace_root=workspace_root,
                        outside_requires_confirmation=True,
                    ):
                        return True
        if (
            words[0] in {"python", "python3", "node", "ruby", "perl"}
            and ("-c" in segment[1:] or "-e" in segment[1:])
            and _INLINE_DESTRUCTIVE_CODE.search(text)
        ):
            return True
        if words[0] == "tee":
            return True
        if words[0] in {"cp", "copy-item", "copy"} and permission_mode == "danger-full-access":
            operands = [
                token for token in segment[1:] if not token.startswith("-") and token != "--"
            ]
            if operands:
                destination = Path(operands[-1]).expanduser()
                if not destination.is_absolute() and workspace_root is not None:
                    base = workspace_root
                    if cwd:
                        requested_cwd = Path(cwd).expanduser()
                        base = (
                            requested_cwd
                            if requested_cwd.is_absolute()
                            else workspace_root / requested_cwd
                        )
                    destination = base / destination
                if _path_requires_confirmation(
                    str(destination),
                    permission_mode=permission_mode,
                    workspace_root=workspace_root,
                ):
                    return True
        if (
            words[0]
            in {
                "add-content",
                "mkdir",
                "new-item",
                "out-file",
                "sed",
                "set-content",
                "touch",
                "truncate",
                "ln",
            }
            and permission_mode == "danger-full-access"
        ):
            if words[0] != "sed" or any(token in {"-i", "--in-place"} for token in words[1:]):
                target_values: list[str] = []
                if words[0] == "sed":
                    target_values = [token for token in segment[1:] if not token.startswith("-")][
                        -1:
                    ]
                if words[0] in {"add-content", "new-item", "out-file", "set-content"}:
                    for index, token in enumerate(words[1:-1], start=1):
                        if token in {"-path", "-literalpath", "-filepath"}:
                            target_values.append(segment[index + 1])
                if not target_values:
                    target_values = [token for token in segment[1:] if not token.startswith("-")]
                for target_value in target_values:
                    target = Path(target_value.strip("\"'")).expanduser()
                    if not target.is_absolute() and workspace_root is not None:
                        base = workspace_root
                        if cwd:
                            requested_cwd = Path(cwd).expanduser()
                            base = (
                                requested_cwd
                                if requested_cwd.is_absolute()
                                else workspace_root / requested_cwd
                            )
                        target = base / target
                    if _path_requires_confirmation(
                        str(target),
                        permission_mode=permission_mode,
                        workspace_root=workspace_root,
                    ):
                        return True
        if ">" in segment or ">>" in segment:
            targets = [
                segment[index + 1]
                for index, value in enumerate(segment[:-1])
                if value in {">", ">>"}
            ]
            if any(protected_host_path_reason(Path(target.strip("\"'"))) for target in targets):
                return True
        if words[0] in {"systemctl", "service"} and any(
            token in {"edit", "enable", "disable", "mask", "start", "stop", "restart"}
            for token in words[1:]
        ):
            return True
    return False


def _path_requires_confirmation(
    raw_path: str,
    *,
    permission_mode: str | None,
    workspace_root: Path | None,
    recursive: bool = False,
    outside_requires_confirmation: bool = False,
) -> bool:
    """Return whether a file operation targets a sensitive or broad path."""
    if recursive:
        return True
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() and workspace_root is not None:
        candidate = workspace_root / candidate
    if protected_host_path_reason(candidate) is not None:
        return permission_mode == "danger-full-access"
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return True
    if ".git" in {part.lower() for part in resolved.parts}:
        return True
    if permission_mode == "danger-full-access" and workspace_root is not None:
        try:
            root = workspace_root.expanduser().resolve(strict=False)
            resolved.relative_to(root)
        except ValueError:
            # Destructive operations against files outside the selected
            # workspace need a deliberate check, even in Full access.
            return outside_requires_confirmation
        except (OSError, RuntimeError):
            return True
        # Deleting or replacing an ancestor could remove the workspace itself.
        if outside_requires_confirmation:
            try:
                root.relative_to(resolved)
            except ValueError:
                pass
            else:
                return True
    return False


def tool_requires_human_confirmation(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    permission_mode: str | None = None,
    workspace_root: Path | None = None,
) -> bool:
    """Return whether a native tool call needs an explicit user decision."""
    if tool_name == "fs_delete":
        path = str(arguments.get("path") or "")
        return _path_requires_confirmation(
            path,
            permission_mode=permission_mode,
            workspace_root=workspace_root,
            recursive=bool(arguments.get("recursive")),
            outside_requires_confirmation=True,
        )
    if tool_name == "delete" or tool_name.endswith(("_delete", "_remove", "_uninstall")):
        # Non-native delete handlers are opaque here (extensions, schedules,
        # remote filesystems), so retain their explicit confirmation gate.
        return True
    if tool_name == "fs_edit":
        operation = str(arguments.get("operation") or "").lower()
        if operation in {"rename", "move"}:
            return any(
                _path_requires_confirmation(
                    str(value),
                    permission_mode=permission_mode,
                    workspace_root=workspace_root,
                    outside_requires_confirmation=True,
                )
                for value in (arguments.get("path"), arguments.get("new_path"))
                if isinstance(value, str) and value.strip()
            )
    if tool_name in {"bash", "pwsh"}:
        command = str(arguments.get("command") or "")
        return shell_command_needs_confirmation(
            command,
            permission_mode=permission_mode,
            workspace_root=workspace_root,
            cwd=str(arguments.get("cwd") or "") or None,
        )
    if tool_name == "terminal_open":
        command = str(arguments.get("command") or "")
        return shell_command_needs_confirmation(
            command,
            permission_mode=permission_mode,
            workspace_root=workspace_root,
            cwd=str(arguments.get("cwd") or "") or None,
        )
    if tool_name == "terminal_write":
        command = str(arguments.get("data") or "")
        return shell_command_needs_confirmation(
            command,
            permission_mode=permission_mode,
            workspace_root=workspace_root,
        )
    if tool_name in {"ssh_run", "ssh_terminal_write"}:
        command = str(arguments.get("command") or arguments.get("data") or "")
        return shell_command_needs_confirmation(command) or not is_known_read_only_command(command)
    if tool_name in {"ssh_fs_write", "ssh_fs_delete"}:
        return True
    if tool_name in {
        "ssh_terminal_open",
        "ssh_terminal_signal",
        "ssh_terminal_close",
        "ssh_stream_close",
    }:
        return permission_mode == "danger-full-access"
    if tool_name.startswith("browser_"):
        if tool_name == "browser_evaluate":
            # Page JavaScript is arbitrary code with access to the active site.
            return permission_mode == "danger-full-access"
        if tool_name not in {
            "browser_click",
            "browser_type",
            "browser_fill",
            "browser_select",
            "browser_drag_drop",
        }:
            return False
        supplied_text = " ".join(
            str(arguments.get(key) or "")
            for key in ("selector", "source_selector", "target_selector", "text", "value", "values")
        )
        return permission_mode == "danger-full-access" and bool(
            _HIGH_IMPACT_TERMS.search(supplied_text)
        )
    if tool_name == "computer_type":
        text = str(arguments.get("text") or "")
        return permission_mode == "danger-full-access" and bool(
            _HIGH_IMPACT_TERMS.search(text)
            or _INLINE_DESTRUCTIVE_CODE.search(text)
            or shell_command_needs_confirmation(text)
        )
    if tool_name == "computer_hotkey":
        keys = {str(key).strip().lower() for key in arguments.get("keys", [])}
        risky_shortcuts = (
            {"ctrl", "alt", "delete"},
            {"ctrl", "shift", "escape"},
            {"alt", "f4"},
            {"win", "r"},
            {"meta", "q"},
            {"command", "q"},
            {"command", "option", "escape"},
        )
        return permission_mode == "danger-full-access" and any(
            shortcut.issubset(keys) for shortcut in risky_shortcuts
        )
    path_value = arguments.get("path")
    if tool_name in {"fs_write", "fs_edit", "fs_str_replace"} and isinstance(path_value, str):
        return _path_requires_confirmation(
            path_value,
            permission_mode=permission_mode,
            workspace_root=workspace_root,
        )
    return False


def is_known_read_only_command(command: str) -> bool:
    """Allow simple inspection commands without review in Full access."""
    text = str(command or "").strip().rstrip("\r\n")
    if not text or re.search(r"[;&|<>`$\n]", text):
        return False
    try:
        tokens = shlex.split(text, posix=os.name != "nt")
    except ValueError:
        return False
    if not tokens:
        return False
    if any(
        token == "-o" or token.startswith("--output=") or token.startswith("--output-")
        for token in tokens[1:]
    ):
        return False
    words = [Path(token.strip("\"'")).name.lower() for token in tokens]
    if words[0] in _READ_ONLY_COMMANDS:
        return True
    if words[0] != "git" or len(words) < 2:
        return False
    git_command = words[1]
    git_arguments = {token.lower() for token in tokens[2:]}
    if git_command in {"diff", "log", "show", "status"}:
        # These options can write a file or invoke an external diff driver.
        return not bool(git_arguments & {"--ext-diff", "--textconv", "--output"})
    if git_command == "branch":
        # `git branch` has several mutating forms; allow only its display
        # options in read-only mode.
        mutating_branch_options = {
            "-d",
            "-D",
            "-m",
            "-M",
            "-c",
            "-C",
            "--delete",
            "--move",
            "--copy",
            "--set-upstream-to",
            "--unset-upstream",
            "--edit-description",
        }
        short_mutating_options = {"-d", "-D", "-m", "-M", "-c", "-C"}
        return not any(
            token in mutating_branch_options
            or any(
                token.startswith(option) and len(token) > len(option)
                for option in short_mutating_options
            )
            or any(
                token.lower().startswith(option + "=")
                for option in mutating_branch_options
                if option.startswith("--")
            )
            for token in tokens[2:]
        )
    return False


def tool_confirmation_warning(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    permission_mode: str | None = None,
    workspace_root: Path | None = None,
) -> str:
    """Describe the concrete impact a user should review before approval."""
    name = str(tool_name or "").strip().lower()
    path = str(arguments.get("path") or arguments.get("new_path") or "").strip()
    recursive = bool(arguments.get("recursive"))

    if name in {"delete", "fs_delete"} or name.endswith("_delete"):
        target = f" `{path}`" if path else " the requested target"
        scope = " and everything inside it" if recursive else ""
        return (
            f"This will permanently delete{target}{scope}. Check the target carefully; "
            "the deleted data may not be recoverable."
        )
    if name in {"fs_edit"} and str(arguments.get("operation") or "").lower() in {"move", "rename"}:
        destination = str(arguments.get("new_path") or "the requested destination").strip()
        return f"This will move or rename `{path or 'the selected file'}` to `{destination}`."
    if path and (
        protected_host_path_reason(Path(path).expanduser()) is not None
        or ".git" in {part.lower() for part in Path(path).parts}
    ):
        return f"This action targets protected path `{path}`. Review the system or Git data it may change."
    if name in {"bash", "pwsh", "terminal_open", "terminal_write"}:
        command = str(arguments.get("command") or arguments.get("data") or "").strip()
        target = f" Command: {command[:240]}" if command else ""
        if permission_mode == "danger-full-access":
            return (
                "This command runs with the desktop user's operating-system permissions and can "
                "change files beyond the workspace. Review the command and working directory "
                f"before allowing it.{target}"
            )
        return f"This command may change or remove workspace data. Review it before allowing it.{target}"
    if name.startswith(("browser_", "computer_", "ssh_")):
        return "This action may change a website, desktop session, or remote machine. Review its target and inputs before allowing it."
    if permission_mode == "danger-full-access" and path and workspace_root is not None:
        try:
            resolved = resolve_conversation_path(workspace_root, path, permission_mode)
            resolved.absolute.relative_to(workspace_root.resolve())
        except ValueError:
            return f"This action targets host path `{path}` outside the selected workspace. Review its contents and purpose before allowing it."
        except (OSError, RuntimeError, WorkspacePathError):
            pass
    if permission_mode == "danger-full-access":
        return "This action can change host or external state. Review the target and inputs before allowing it."
    return "This action can change workspace data. Review the target and inputs before allowing it."


__all__ = [
    "blocked_shell_command_reason",
    "is_known_read_only_command",
    "protected_host_path_reason",
    "resolve_conversation_path",
    "shell_command_needs_confirmation",
    "tool_confirmation_warning",
    "tool_requires_human_confirmation",
]

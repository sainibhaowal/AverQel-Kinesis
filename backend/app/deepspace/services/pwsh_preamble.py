"""PowerShell encoding preamble and prompt bootstrap for DeepSpace.

Python-native port of the DeepSeek Harness pwsh encoding preamble +
prompt function setup, ensuring reliable pwsh startup.
"""

from __future__ import annotations

# UTF-8 BOM + encoding preamble for pwsh
ENCODING_PREAMBLE = "\ufeff"  # UTF-8 BOM

# PowerShell prompt function setup that emits controlled prompt markers
PWSH_PROMPT_SETUP = r"""
function prompt {
    $marker = [char]27 + "]133;D;" + [char]7
    Write-Host -NoNewline $marker
    "dsh> "
}
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
"""

# Bash controlled prompt setup
BASH_PROMPT_SETUP = r"""
__dsh_prompt() {
    printf '\e]133;D;\a'
    printf 'dsh> '
}
PROMPT_COMMAND='__dsh_prompt'
"""

# OSC 133;D; marker prefix
OSC_MARKER_PREFIX = "133;D;"


def build_pwsh_startup_args() -> list[str]:
    """Return pwsh startup arguments with encoding preamble."""
    return ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass"]


def build_pwsh_command_with_preamble(command: str) -> str:
    """Wrap a command with the pwsh encoding preamble."""
    return f"{ENCODING_PREAMBLE}{PWSH_PROMPT_SETUP}\n{command}"


def build_bash_startup_args() -> list[str]:
    """Return bash startup arguments with controlled prompt."""
    return ["--noprofile", "--norc", "-i"]


def get_controlled_prompt() -> str:
    """Return the controlled prompt string."""
    return "dsh> "


def get_prompt_marker() -> str:
    """Return the OSC prompt marker."""
    return "\x1b]133;D;\x07"

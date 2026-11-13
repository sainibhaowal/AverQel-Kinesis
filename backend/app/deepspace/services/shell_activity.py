"""Shell activity tracking for DeepSpace interactive terminals.

Python-native port of the AverQel KineSis ShellActivity. Provides opt-in
startup integration and revision tracking for interactive bash/zsh shells.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass


@dataclass
class ShellActivityState:
    state: str = "unknown"  # idle | busy | unknown
    revision: int = 0


class ShellActivity:
    """Opt-in startup integration and revision tracking for one interactive shell."""

    def __init__(self, directory: str, argv: list[str], env: dict[str, str]) -> None:
        self._directory = directory
        self.argv = argv
        self.env = env
        self._revision = 0
        self._observed = ""
        self._invalidated: str | None = None
        self._state = "unknown"

    def invalidate(self) -> None:
        self._invalidated = self._read()
        self._revision += 1
        self._state = "unknown"

    def inspect(self, pid: int) -> ShellActivityState:
        record = self._read()
        if record != self._observed:
            self._observed = record
            self._revision += 1
        import re

        m = re.match(r"^(\d+):(\d+):(idle|busy)\n?$", record)
        if record == self._invalidated or (m is not None and m.group(1) != str(pid)):
            self._state = "unknown"
        elif m is not None:
            self._state = m.group(3)
        else:
            self._state = "unknown"
        return ShellActivityState(state=self._state, revision=self._revision)

    def dispose(self) -> None:
        import shutil

        try:
            shutil.rmtree(self._directory, ignore_errors=True)
        except Exception:
            pass

    def _read(self) -> str:
        try:
            with open(os.path.join(self._directory, "state"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def prepare_shell_activity(
    shell_path: str,
    shell_args: list[str],
    env: dict[str, str],
    platform: str = "linux",
    enabled: bool = False,
) -> ShellActivity | None:
    """Prepare optional Bash/Zsh integration for an interactive launch.

    Returns None for unsupported launches (non-bash/zsh, Windows, etc.).
    """
    if not enabled:
        return None
    if platform == "win32":
        return None
    if len(shell_args) != 1 or shell_args[0] != "-i":
        return None
    import os as _os

    shell = _os.path.basename(shell_path)
    if shell not in ("bash", "zsh"):
        return None

    directory = tempfile.mkdtemp(prefix="dsh-shell-")
    state_path = _quote(os.path.join(directory, "state"))
    guards_path = _quote(os.path.join(directory, "guards"))

    try:
        if shell == "bash":
            rc = os.path.join(directory, "bashrc")
            content = "\n".join(
                [
                    "[[ ! -r ~/.bashrc ]] || builtin source ~/.bashrc",
                    "if (( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4) )) && [[ ! $(declare -p PROMPT_COMMAND PS0 2>/dev/null) =~ declare\\ -[^[:space:]]*r ]]; then",
                    "  __dsh_shell_pid=$BASHPID; __dsh_shell_sequence=0",
                    "  __dsh_shell_idle() {",
                    "    local result=$?",
                    '    if [[ $BASHPID == "$__dsh_shell_pid" ]]; then',
                    "      (( ++__dsh_shell_sequence ))",
                    "      local activity=idle",
                    f"      builtin trap -p >| {guards_path}",
                    f"      [[ ! -s {guards_path} ]] || activity=unknown",
                    f'      builtin printf \'%s:%s:%s\\n\' "$BASHPID" "$__dsh_shell_sequence" "$activity" >| {state_path}',
                    "    fi",
                    '    return "$result"',
                    "  }",
                    '  if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == "declare -a "* ]]; then',
                    "    PROMPT_COMMAND+=(__dsh_shell_idle)",
                    "  else",
                    '    PROMPT_COMMAND="${PROMPT_COMMAND}"$\'\\n\'"__dsh_shell_idle"',
                    "  fi",
                    "  PS0+="
                    + _quote(
                        '$(builtin printf \'%s:%s:busy\' "$__dsh_shell_pid" "$__dsh_shell_sequence" >| '
                        + os.path.join(directory, "state")
                        + ")"
                    ),
                    "fi",
                    "",
                ]
            )
            with open(rc, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(rc, 0o600)
            new_argv = [shell_path, "--rcfile", rc, "-i"]
            return ShellActivity(directory, new_argv, env)
        else:  # zsh
            zshenv = os.path.join(directory, ".zshenv")
            content = "\n".join(
                [
                    (
                        "unset ZDOTDIR"
                        if env.get("ZDOTDIR") is None
                        else f"ZDOTDIR={_quote(env['ZDOTDIR'])}"
                    ),
                    '[[ ! -r ${ZDOTDIR:-$HOME}/.zshenv ]] || builtin source "${ZDOTDIR:-$HOME}/.zshenv"',
                    "typeset -g __dsh_shell_pid=$$ __dsh_shell_sequence=0",
                    "__dsh_shell_activity() {",
                    "  (( ZSH_SUBSHELL == 0 && $$ == __dsh_shell_pid )) || return",
                    "  (( ++__dsh_shell_sequence ))",
                    f'  builtin printf \'%s:%s:%s\\n\' "$$" "$__dsh_shell_sequence" "$1" >| {state_path}',
                    "  return 0",
                    "}",
                    "__dsh_shell_idle() {",
                    f"  {{ builtin trap; zle -F; }} >| {guards_path}",
                    "  if [[ $CONTEXT != start || -n $BUFFER ]]; then __dsh_shell_activity busy",
                    f"  elif [[ -s {guards_path} || -n ${{(k)functions[(I)TRAP*]}} ]]; then __dsh_shell_activity unknown",
                    "  else __dsh_shell_activity idle; fi",
                    "}",
                    "__dsh_shell_busy() { __dsh_shell_activity busy }",
                    "__dsh_shell_init() {",
                    "  autoload -Uz add-zle-hook-widget add-zsh-hook",
                    "  add-zle-hook-widget line-init __dsh_shell_idle",
                    "  add-zle-hook-widget line-finish __dsh_shell_busy",
                    "  add-zsh-hook preexec __dsh_shell_busy",
                    "  precmd_functions=(${precmd_functions:#__dsh_shell_init})",
                    "}",
                    "typeset -ga precmd_functions",
                    "precmd_functions+=(__dsh_shell_init)",
                    "",
                ]
            )
            with open(zshenv, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(zshenv, 0o600)
            new_env = {**env, "ZDOTDIR": directory}
            return ShellActivity(directory, shell_args, new_env)
    except Exception:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
        raise

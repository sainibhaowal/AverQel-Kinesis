# 48. Source-aware launch environment

## Use case

An operator wants inherited process settings to override launch-directory
configuration, with user-level defaults available when the launch has no
explicit value. A runtime also needs to know which source supplied a value
before using it for a sensitive lookup.

## Why it matters

Blindly merging environment files can silently replace an operator's explicit
settings, and evaluating a `.env` file as shell code would turn configuration
into code execution.

## How it works

`app/core/launch_environment.py` captures an immutable snapshot when first
requested. Precedence is process environment, launch working directory
`.env`, then user `~/.env`. Lookups are case-insensitive, retain the winning
source, and may restrict accepted sources (for example, to process-only
values). `launched_through_ssh` checks inherited `SSH_CONNECTION` or
`SSH_TTY` only; dotenv values do not make a process appear to be SSH-launched.

The dotenv reader accepts ordinary single-line `KEY=value` assignments,
optional matching quotes, `export KEY=value`, and comments. It does not expand
variables, interpret shell syntax, execute commands, or support multiline
values. The snapshot is used by the shared proxy policy and trusted
product-owned child-process setup. It is separate from Pydantic `Settings`;
it does not make deployment secrets available to shell, PTC, or browser
subprocesses.

## What to watch for

- Do not log environment values or serialize the snapshot into model-visible
  tool output. Diagnostics should report source or presence, not secret values.
- Process values outrank both files. Project `.env` outranks user `~/.env`.
- Shell, terminal, PTC, and browser environments remain reduced or explicitly
  isolated; this snapshot does not grant them the backend's environment.
- Focused tests are in `backend/tests/unit/test_launch_environment.py`.

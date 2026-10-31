#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
FRONTEND_STATE_DIR="${AVERQEL_FRONTEND_STATE_DIR:-$FRONTEND_DIR/.local}"
RUNTIME_ENV_FILE="$FRONTEND_STATE_DIR/.env.local"

# The runtime file supplies local defaults (including E2E-only settings), but
# an explicit value from a launcher must win. Desktop startup injects its
# isolated API and renderer ports this way; allowing the runtime file to
# overwrite them makes the renderer call a different, usually stopped,
# backend.
declare -a EXPLICIT_ENV_NAMES
declare -a EXPLICIT_ENV_VALUES
EXPLICIT_ENV_COUNT=0
for name in \
  PORT \
  BUILD_TARGET \
  STANDALONE \
  PLAYWRIGHT_E2E \
  NEXT_PUBLIC_API_URL \
  NEXT_PUBLIC_BUILD_TARGET \
  NEXT_PUBLIC_E2E_DISABLE_REALTIME
do
  if [[ -n "${!name+x}" ]]; then
    EXPLICIT_ENV_NAMES[$EXPLICIT_ENV_COUNT]="$name"
    EXPLICIT_ENV_VALUES[$EXPLICIT_ENV_COUNT]="${!name}"
    EXPLICIT_ENV_COUNT=$((EXPLICIT_ENV_COUNT + 1))
  fi
done

if [[ -f "$RUNTIME_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$RUNTIME_ENV_FILE"
  set +a
fi

for ((index = 0; index < EXPLICIT_ENV_COUNT; index++)); do
  export "${EXPLICIT_ENV_NAMES[$index]}=${EXPLICIT_ENV_VALUES[$index]}"
done

exec bash -c "$1"

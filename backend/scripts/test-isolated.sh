#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$BACKEND_DIR/ops/test/compose.yml"
PROJECT="averqel-test-${BASHPID}-${RANDOM}"
ISOLATED_BASE_IMAGE="averqel-isolated-test-base:latest"
TEST_BASE_IMAGE="$ISOLATED_BASE_IMAGE"
TEST_BASE_STATE="$BACKEND_DIR/.cache/isolated-test-base.sha256"
TEST_BASE_INPUT_HASH="$(
  {
    sha256sum "$BACKEND_DIR/Dockerfile.base" | cut -d' ' -f1
    sha256sum "$BACKEND_DIR/requirements.txt" | cut -d' ' -f1
  } | sha256sum | cut -d' ' -f1
)"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for isolated database-backed tests." >&2
  exit 2
fi

docker info >/dev/null

compose=(docker compose --env-file /dev/null --project-name "$PROJECT" --file "$COMPOSE_FILE")

cleanup() {
  local status=$?
  trap - EXIT
  "${compose[@]}" down --volumes --remove-orphans --timeout 5 >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

# Use a separately tagged base built from the current sources. A pre-existing
# production image may be stale and omit tools required by the full test suite.
if { ! docker image inspect "$TEST_BASE_IMAGE" >/dev/null 2>&1 \
  || [[ ! -f "$TEST_BASE_STATE" ]] \
  || [[ "$(cat "$TEST_BASE_STATE")" != "$TEST_BASE_INPUT_HASH" ]]; }; then
  docker build \
    --file "$BACKEND_DIR/Dockerfile.base" \
    --tag "$ISOLATED_BASE_IMAGE" \
    "$BACKEND_DIR"
  mkdir -p "$(dirname -- "$TEST_BASE_STATE")"
  printf '%s\n' "$TEST_BASE_INPUT_HASH" > "$TEST_BASE_STATE"
fi

"${compose[@]}" build --build-arg "TEST_BASE_IMAGE=$TEST_BASE_IMAGE" backend-tests
"${compose[@]}" run --rm backend-tests "$@"

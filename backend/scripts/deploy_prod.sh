#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$ROOT_DIR/.env.vps}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Production env file not found. Expected $ROOT_DIR/.env.vps or an explicit env file path as the first argument"
  exit 1
fi

"$ROOT_DIR/scripts/validate_prod_env.sh" "$ENV_FILE"
chmod 600 "$ENV_FILE"
docker compose -f "$ROOT_DIR/docker-compose.prod.yml" --env-file "$ENV_FILE" config >/dev/null

# The API and worker images are thin wrappers around a base image that holds
# the runtime, so the base is built first and the services are built on top
# of it.  One build serves both: the base is the same image, under two names.
docker build --file "$ROOT_DIR/Dockerfile.base" --tag backend-api-base:latest "$ROOT_DIR"
docker tag backend-api-base:latest backend-worker-base:latest

docker compose -f "$ROOT_DIR/docker-compose.prod.yml" --env-file "$ENV_FILE" pull || true
docker compose -f "$ROOT_DIR/docker-compose.prod.yml" --env-file "$ENV_FILE" up -d --build --remove-orphans
docker compose -f "$ROOT_DIR/docker-compose.prod.yml" --env-file "$ENV_FILE" ps

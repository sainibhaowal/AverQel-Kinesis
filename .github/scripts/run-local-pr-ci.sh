#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(git rev-parse --show-toplevel)"
export repo_root
run_precommit=1

usage() {
  cat <<'USAGE'
Usage: bash .github/scripts/run-local-pr-ci.sh [--skip-pre-commit]

Runs the locally reproducible pull-request CI jobs: pre-commit, workflow lint,
backend quality, frontend quality, isolated browser E2E, and desktop checks.
GitHub dependency review is reported as hosted-only. Tool-calling replay is
skipped with an explicit notice until all committed fixture baselines exist.
USAGE
}

while (($#)); do
  case "$1" in
    --skip-pre-commit)
      run_precommit=0
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

cd "$repo_root"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required local CI command: $1" >&2
    return 1
  fi
}

require_file() {
  if [[ ! -x "$1" ]]; then
    echo "Missing executable $1. Install the CI dependencies documented in CONTRIBUTING.md." >&2
    return 1
  fi
}

for command_name in actionlint pnpm setsid; do
  require_command "$command_name"
done

for executable in \
  backend/.venv/bin/pytest \
  backend/.venv/bin/ruff \
  backend/.venv/bin/black \
  backend/.venv/bin/mypy \
  backend/.venv/bin/bandit \
  backend/.venv/bin/pip-audit \
  backend/.venv/bin/safety \
  frontend/node_modules/.bin/eslint \
  frontend/node_modules/.bin/playwright \
  applications/desktop/node_modules/.bin/electron
do
  require_file "$executable"
done

python_version="$(backend/.venv/bin/python --version 2>&1)"
if [[ ! "$python_version" =~ ^Python\ 3\.12\. ]]; then
  echo "Backend CI requires Python 3.12; found: $python_version" >&2
  exit 1
fi

if ((run_precommit)); then
  require_command pre-commit
  pre-commit run --all-files
fi

export CI=true
export NEXT_TELEMETRY_DISABLED=1
export PYTEST_XDIST_AUTO_NUM_WORKERS="${PYTEST_XDIST_AUTO_NUM_WORKERS:-4}"
ci_cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/averqel-kinesis"
export AVERQEL_BUILD_PYTHON_ENV="${AVERQEL_BUILD_PYTHON_ENV:-$ci_cache_root/desktop-build-venv}"
export AVERQEL_PLAYWRIGHT_BROWSERS_PATH="${AVERQEL_PLAYWRIGHT_BROWSERS_PATH:-$ci_cache_root/desktop-playwright-browsers}"
ci_temp_root="${AVERQEL_LOCAL_CI_TMPDIR:-$ci_cache_root/tmp}"
mkdir -p "$ci_temp_root"
export TMPDIR="$ci_temp_root"

temp_root="$(mktemp -d "$ci_temp_root/averqel-local-pr-ci.XXXXXX")"
declare -a job_names=(workflow-lint backend-quality frontend-quality browser-e2e desktop)
declare -a job_pids=()
failed=0

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if ((status != 0)); then
    for job_pid in "${job_pids[@]}"; do
      [[ -n "$job_pid" ]] || continue
      kill -TERM -- "-$job_pid" 2>/dev/null || kill -TERM "$job_pid" 2>/dev/null || true
    done
    sleep 1
    for job_pid in "${job_pids[@]}"; do
      [[ -n "$job_pid" ]] || continue
      kill -KILL -- "-$job_pid" 2>/dev/null || true
      wait "$job_pid" 2>/dev/null || true
    done
  fi
  python3 - "$temp_root" <<'PY'
from pathlib import Path
import shutil
import sys

shutil.rmtree(Path(sys.argv[1]), ignore_errors=True)
PY
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_job() {
  local job_name="$1"
  local job_function="$2"
  local log_file="$temp_root/$job_name.log"

  export -f "$job_function"
  (
    exec setsid bash -c 'set -Eeuo pipefail; "$1"' bash "$job_function"
  ) >"$log_file" 2>&1 &
  job_pids+=("$!")
}

workflow_lint_job() {
  actionlint -color
  git diff --check
}

backend_quality_job() {
  cd "$repo_root/backend"
  # GitHub Actions prepends backend/.venv/bin to GITHUB_PATH. Mirror that in
  # the local runner so every gate uses the pinned CI tool versions.
  export PATH="$repo_root/backend/.venv/bin:$PATH"
  bash ../.github/scripts/run-backend-gate.sh ruff
  bash ../.github/scripts/run-backend-gate.sh black
  bash ../.github/scripts/run-backend-gate.sh mypy
  bash ../.github/scripts/run-backend-gate.sh bandit
  bash ../.github/scripts/run-backend-gate.sh pip-audit

  if bash ../.github/scripts/run-backend-gate.sh safety; then
    :
  else
    echo "Safety is advisory in pull-request CI; review its findings above."
  fi

  PATH="/usr/bin:/bin:$PATH" .venv/bin/pytest -q -m unit_no_db --dist=loadgroup
}

frontend_quality_job() {
  pnpm --dir frontend lint
  pnpm --dir frontend test
  pnpm --dir frontend build
  pnpm --dir frontend audit --prod --audit-level high
}

browser_e2e_job() {
  pnpm --dir frontend exec playwright install chromium
  env \
    -u DEEPSPACE_E2E_BASE_URL \
    -u DEEPSPACE_E2E_ALLOW_EXTERNAL \
    -u DEEPSPACE_E2E_STORAGE_STATE \
    -u DEEPSPACE_E2E_AUTH_TOKEN \
    pnpm --dir frontend e2e
}

desktop_job() (
  local desktop_temp_root
  desktop_temp_root="$(mktemp -d "${TMPDIR:-$HOME/.cache/averqel-kinesis/tmp}/averqel-desktop-ci.XXXXXX")"
  trap 'python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$desktop_temp_root"' EXIT
  export TMPDIR="$desktop_temp_root"
  pnpm --dir applications/desktop test
  pnpm --dir applications/desktop build:backend
  pnpm --dir applications/desktop smoke:backend
)

echo "Running the locally reproducible pull-request CI jobs in parallel."
echo "The GitHub-only dependency review job will be reported as hosted-only."

start_job workflow-lint workflow_lint_job
start_job backend-quality backend_quality_job
start_job frontend-quality frontend_quality_job
start_job browser-e2e browser_e2e_job
start_job desktop desktop_job

# Named background functions above are wrapped to keep their output isolated.
wait_for_jobs() {
  local job_index=0
  for job_name in "${job_names[@]}"; do
    job_pid="${job_pids[$job_index]}"
    if wait "$job_pid"; then
      printf 'PASS %s\n' "$job_name"
    else
      printf 'FAIL %s\n' "$job_name"
      failed=1
    fi
    job_pids[$job_index]=""
    job_index=$((job_index + 1))
  done
}

wait_for_jobs

for job_name in "${job_names[@]}"; do
  log_file="$temp_root/$job_name.log"
  if [[ -f "$log_file" ]]; then
    printf '\n--- %s (last 35 lines) ---\n' "$job_name"
    tail -n 35 "$log_file"
  fi
done

fixture_dir="backend/tests/tool_calling/fixtures"
fixture_missing=()
for category in core advanced chaining error; do
  fixture_file="$fixture_dir/${category}_tools.jsonl"
  if [[ ! -s "$fixture_file" ]]; then
    fixture_missing+=("$fixture_file")
  fi
done

if ((${#fixture_missing[@]})); then
  echo "SKIP tool-calling fixture replay: committed baselines are missing or empty:"
  printf '  - %s\n' "${fixture_missing[@]}"
  echo "Record and review disposable fixtures before claiming tool-calling replay coverage."
else
  echo "Running tool-calling fixture validation and replay."
  (
    cd backend
    .venv/bin/python -m tests.tool_calling.run_replay \
      --validate --fixture-dir tests/tool_calling/fixtures
    .venv/bin/python -m tests.tool_calling.check_fixture_coverage \
      --fixture-dir tests/tool_calling/fixtures
    .venv/bin/python -m tests.tool_calling.run_replay \
      --category all --workers 2 --fixture-dir tests/tool_calling/fixtures
  ) || failed=1
fi

echo "SKIP dependency review: GitHub's pull-request API and repository graph are required."

if ((failed)); then
  echo "Local pull-request CI failed. Do not push until the reported failures are fixed." >&2
  exit 1
fi

echo "All locally runnable pull-request CI checks passed."

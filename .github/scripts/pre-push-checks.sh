#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(git rev-parse --show-toplevel)"

if [[ "$#" -ne 2 ]]; then
  echo "Git pre-push hook must receive the remote name and URL." >&2
  exit 2
fi

validate_commit() (
  set -Eeuo pipefail

  local commit="$1"
  local temp_root
  local checkout
  local cache_root
  local precommit_status=0
  local local_ci_status=0

  cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/averqel-kinesis/pre-push-worktrees"
  mkdir -p "$cache_root"
  temp_root="$(mktemp -d "$cache_root/averqel-pre-push.XXXXXX")"
  checkout="$temp_root/checkout"

  cleanup() {
    git -C "$repo_root" worktree remove --force "$checkout" >/dev/null 2>&1 || true
    rm -rf -- "$temp_root"
  }
  trap cleanup EXIT

  if ! git -C "$repo_root" worktree add --quiet --detach "$checkout" "$commit"; then
    echo "Could not create an isolated validation worktree for $commit" >&2
    exit 1
  fi

  # Reuse installed Python and desktop tools without exposing the live source
  # tree to hooks that format or otherwise modify files. Keep the worktree on
  # the home filesystem because packaged backend builds exceed common /tmp
  # tmpfs limits.
  for dependency_dir in \
    backend/.venv \
    applications/desktop/node_modules
  do
    source_dir="$repo_root/$dependency_dir"
    target_dir="$checkout/$dependency_dir"
    if [[ -d "$source_dir" && ! -e "$target_dir" && ! -L "$target_dir" ]]; then
      mkdir -p "$(dirname "$target_dir")"
      ln -s "$source_dir" "$target_dir"
    fi
  done

  # Next's production build rejects node_modules symlinks that resolve outside
  # the project root. Install from the committed lockfile in this worktree.
  if ! command -v pnpm >/dev/null 2>&1; then
    echo "pnpm is required to install frontend dependencies in the validation worktree." >&2
    exit 1
  fi
  pnpm --dir "$checkout/frontend" install --frozen-lockfile

  echo "Running repository hygiene and formatting checks against $commit..."
  if (cd "$checkout" && pre-commit run --all-files --hook-stage pre-commit); then
    precommit_status=0
  else
    precommit_status=$?
  fi

  echo "Running full local pull-request CI against $commit..."
  if (cd "$checkout" && pre-commit run --all-files --hook-stage pre-push); then
    local_ci_status=0
  else
    local_ci_status=$?
  fi

  if (( precommit_status != 0 || local_ci_status != 0 )); then
    echo "::error::Push validation failed for $commit (pre-commit=$precommit_status, local-ci=$local_ci_status)." >&2
    changed_files="$(git -C "$checkout" status --short)"
    if [[ -n "$changed_files" ]]; then
      echo "Hooks changed files in the disposable worktree; update and recommit them before pushing:" >&2
      printf '%s\n' "$changed_files" >&2
    fi
  fi

  if (( precommit_status != 0 || local_ci_status != 0 )); then
    exit 1
  fi
)

declare -a commits_to_check=()
while IFS=' ' read -r local_ref local_sha remote_ref remote_sha extra; do
  [[ -n "${local_ref:-}" ]] || continue
  if [[ -n "${extra:-}" || -z "${local_sha:-}" || -z "${remote_ref:-}" || -z "${remote_sha:-}" ]]; then
    echo "Invalid input from Git's pre-push hook." >&2
    exit 2
  fi

  # A zero local SHA means the ref is being deleted; there is no commit to validate.
  if [[ "$local_sha" =~ ^0+$ ]]; then
    continue
  fi

  if ! git -C "$repo_root" cat-file -e "${local_sha}^{commit}" 2>/dev/null; then
    echo "The pushed object is not an available commit: $local_sha ($local_ref)" >&2
    exit 1
  fi

  case " ${commits_to_check[*]} " in
    *" $local_sha "*) ;;
    *) commits_to_check+=("$local_sha") ;;
  esac
done

if (( ${#commits_to_check[@]} == 0 )); then
  echo "No commits are being pushed; validation is not needed."
  exit 0
fi

if ! command -v pre-commit >/dev/null 2>&1; then
  echo "pre-commit is required for the push gate. Install it with: python -m pip install pre-commit" >&2
  exit 1
fi

if ! command -v actionlint >/dev/null 2>&1; then
  echo "Actionlint is required for local pull-request CI. Install it locally before pushing." >&2
  exit 1
fi

for commit in "${commits_to_check[@]}"; do
  validate_commit "$commit"
done

echo "All pushed commits passed local pull-request CI."

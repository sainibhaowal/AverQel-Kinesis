---
name: quality-gates
description: Run and diagnose AverQel's applicable backend, frontend, formatting, type, security, test, documentation, and workflow checks. Use before reporting code changes ready or before any requested push.
---

# Quality-gate workflow

1. Read `AGENT_ENGINEERING_STANDARD.md`, `CONTRIBUTING.md`, and the relevant
   component guide. Review all existing workflows under `.github/workflows/`
   when a workflow changes. The required pull-request workflow is
   `.github/workflows/ci.yml`.
2. Prefer read-only checks. The backend quality-gate commands are defined in
   `.github/scripts/run-backend-gate.sh`. To execute every backend quality
   gate from the repository root, use:

   ```bash
   set -euo pipefail
   cd backend
   for gate in ruff black mypy bandit pip-audit safety pytest; do
     bash ../.github/scripts/run-backend-gate.sh "$gate"
   done
   ```

   The backend pytest gate covers `unit_no_db`. For database-backed tests,
   use `safe-testing` and `./backend/scripts/test-isolated.sh`; include the
   full suite when the change or requested release validation requires it.
3. For frontend changes, common local checks from the repository root are:

   ```bash
   pnpm --dir frontend lint
   pnpm --dir frontend test
   pnpm --dir frontend build
   pnpm --dir frontend audit --prod --audit-level high
   ```

   Run `pnpm --dir frontend e2e` when browser coverage applies. Do not use the
   project-wide `pnpm --dir frontend format` command for a targeted change; it
   writes across the frontend.
4. Ensure `pre-commit`, Actionlint, the Python and pnpm CI environments, and
   the repository pre-push hook are installed before pushing:

   ```bash
   python -m pip install pre-commit
   pre-commit install
   bash .github/scripts/install-pre-push-hook.sh
   ```

   The hook runs formatting and hygiene checks followed by
   `.github/scripts/run-local-pr-ci.sh` in a temporary detached worktree for
   every pushed ref. Formatter changes stay out of the active checkout and
   block the push until corrected and committed. The runner covers backend,
   frontend, browser E2E, desktop, and workflow checks; GitHub Dependency Review
   is hosted-only, and tool replay is skipped until fixture baselines exist.
   After pushing, inspect GitHub Actions for that exact commit. A green result
   from a different SHA does not validate the pushed commit. Do not merge,
   publish, or deploy unless separately authorized.
6. Review documentation impact and update relevant docs. Verify changed
   Markdown links and commands against the repository. Report documentation
   changes or why none were needed.
7. Report exact commands and outcomes, including pass/fail/skip counts, any
   GitHub Actions run for the pushed SHA, gates not run, and environmental
   limits. Do not claim all checks are green unless the complete applicable
   set passed.

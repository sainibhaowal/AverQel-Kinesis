# Contributing to AverQel Kinesis

Thank you for improving AverQel Kinesis. Contributions must preserve tenant isolation,
credential privacy, reliable document processing, and the protected production
delivery path.

Agent-assisted work follows the repository's
[`AGENTS.md`](AGENTS.md) and detailed
[`AGENT_ENGINEERING_STANDARD.md`](AGENT_ENGINEERING_STANDARD.md). The standard
defines plan-first implementation, critical review, required tests,
documentation review, safe test isolation, and pre-push checks.

## Before changing code

1. Create a branch from the latest `main`:

   ```bash
   git switch main
   git pull --ff-only origin main
   git switch -c feat/short-description
   ```

2. Use a descriptive branch prefix such as `feat/`, `fix/`, `docs/`,
   `chore/`, or `security/`.
3. Read the relevant component guide and identify the tests that cover the
   change.
4. Never commit `.env*` files, tokens, private keys, model files, build
   outputs, `node_modules`, `.venv`, or `__pycache__` files.

## Plan, tests, and documentation

- Before implementation, inspect the existing behavior and working tree, then
  write a plan that names the goal, current state, affected files and
  interfaces, risks, non-break requirements, tests, and documentation. For
  routine changes, proceed after presenting the plan; it is not an approval
  gate.
- Add behavior-focused tests for new or changed logic and regression coverage
  for bug fixes. Choose unit, component, API/integration, migration, browser,
  or packaging tests according to the behavior being changed.
- Review documentation impact on every change. Update user help, API,
  configuration, security, testing, operator, or release docs in the same
  change when those details change. If no documentation edit is needed, record
  the reason in the completion report.
- Never use active databases or services for tests. Follow
  [`backend/docs/platform/03-testing.md`](backend/docs/platform/03-testing.md)
  and [`backend/docs/security/isolated-test-runs.md`](backend/docs/security/isolated-test-runs.md).

## Contribution terms

By submitting a contribution, you confirm that you have the right to submit
it and agree that it may be distributed under the [Apache License 2.0](LICENSE).
Do not submit code, documentation, media, or dependencies copied from a source
whose license is incompatible with Apache-2.0. Preserve required third-party
notices and identify material license changes in the pull request.

## Development rules

- Keep API changes tenant-scoped and authenticated.
- Keep OAuth secrets encrypted and out of logs, browser payloads, and prompts.
- Treat destructive actions as approval-gated operations.
- Preserve existing API contracts unless the pull request documents a
  deliberate migration.
- Update tests and documentation with behavior changes.
- Treat dependency upgrades as security-sensitive changes: update the lockfile,
  explain compatibility impact, and run the applicable audit and test checks.
- Do not edit VPS-owned files from the repository; production configuration
  belongs in `/opt/averqel/backend/.env.vps` and related operator-managed paths.

## Local verification

Run focused checks while iterating. Before pushing, the installed pre-push hook
runs a fast local gate in an isolated worktree. It covers workflow lint and the
pre-push hook regression, backend static/security checks plus a focused CI guard
test, frontend lint/tests/build/audit, and Electron shell tests. Run it manually:

```bash
bash .github/scripts/run-local-pr-ci.sh --fast
```

The GitHub `CI Passed` pull-request check remains the merge gate. It also runs
the full backend database-free suite, all browser E2E tests, and the packaged
desktop runtime build/smoke test on supported GitHub runners. To run that full
locally reproducible suite on demand, use `bash .github/scripts/run-local-pr-ci.sh`;
it is intentionally not repeated on every push because those jobs are much
slower and require substantially more disk and CPU.

Set up the pinned Python 3.12 backend environment, frontend and desktop pnpm
dependencies, pre-commit, and Actionlint before the first push. The pre-push
hook installs frontend dependencies from its lockfile inside the
isolated worktree. That worktree is created under `~/.cache/averqel-kinesis`
(or `$XDG_CACHE_HOME`) so the packaged backend build uses the home filesystem
instead of a size-limited `/tmp` tmpfs. Temporary test and build files also use
`~/.cache/averqel-kinesis/tmp` by default; set `AVERQEL_LOCAL_CI_TMPDIR` to
override it. The runner does not install system
packages or contact live application services. GitHub Dependency Review is
hosted-only. Tool-calling replay is explicitly skipped until reviewed JSONL
baselines are committed; the local and hosted summaries report that skip.

The pull-request workflow still runs on GitHub after a push and publishes the
required aggregate `CI Passed` result, including Windows native-runtime
coverage. Local checks do not replace review of the exact commit's Actions
results before merge. Desktop packaging and release runs are separate from PR
CI; see `.github/RELEASE_SECURITY.md` for their triggers and signing needs.

## Pull requests

- Do not push directly to protected `main`; submit a pull request from your
  working branch.
- Use a title such as `feat: add provider health summary`, `fix: prevent ...`,
  or `docs: explain ...`.
- Describe the user-visible behavior, implementation, tests, migrations, and
  operational impact.
- Include screenshots or logs for UI and workflow changes when useful.
- Keep unrelated formatting, generated files, and dependency updates out of
  the change.
- Resolve all review conversations and wait for the required `CI Passed` check
  before merging. Protected `main` must require that check; run
  `.github/configure-main-protection.sh` with a repository-admin token when
  configuring the repository.

Install `pre-commit` and both repository Git hooks once after cloning:

```bash
python -m pip install pre-commit
pre-commit install
bash .github/scripts/install-pre-push-hook.sh
```

The pre-push hook requires the CI dependencies above, `pre-commit`, `pnpm`, and
Actionlint. It installs the frontend dependency tree from the committed lockfile
in its isolated worktree; keep pnpm's package store available or allow package
downloads on the first run. Playwright browser binaries are needed only for the
full local suite; GitHub Actions installs its own browser dependencies.

For every pushed ref, the hook checks the commit in a temporary detached
worktree under `~/.cache/averqel-kinesis/pre-push-worktrees` (or
`$XDG_CACHE_HOME`). It runs formatting and hygiene hooks followed by the fast
local gate. Formatter changes stay out of the active checkout and block the
push until corrected and committed. GitHub's full PR workflow retains every
merge-blocking job, including the complete backend suite, browser E2E, and
packaged-runtime smoke. Hosted dependency review also remains in GitHub Actions.
Inspect the exact commit's Actions results before merging.

Only reviewed pull requests may enter protected `main`. Release and VPS
deployment workflows are intentionally manual and are not triggered by an
ordinary pull request merge.

## Commit style

Use an imperative Conventional Commit style:

```text
feat(scope): add a user-visible capability
fix(scope): correct an observable failure
docs(scope): clarify an operational procedure
chore(scope): maintain tooling without product behavior changes
```

Breaking changes must be documented in the commit body with `BREAKING CHANGE:`
so semantic release calculation can identify them.

Pull request titles are also required to use Conventional Commit format because
the automatic release calculation reads commit subjects. With squash merging,
GitHub uses the pull request title as the resulting commit subject. Use `feat:`
for a backward-compatible feature, `fix:` for a bug fix, and the appropriate
other conventional type for maintenance work. Mark a breaking change with `!`
in the title, for example `feat(api)!: remove the legacy endpoint`; this keeps
the breaking-change signal in the squash commit subject. An unrecognized or
maintenance-only type does not cause an automatic release by itself.

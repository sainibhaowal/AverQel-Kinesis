# Testing the backend safely

Use database-free tests for normal code changes. Database-backed and live-provider tests run only against isolated, disposable services.

## Database-free tests

From the repository root, run:

```bash
cd backend
pytest -m unit_no_db
```

`unit_no_db` tests use fake or in-memory dependencies. The test bootstrap replaces inherited database, Redis, and object-store URLs with unreachable test-only hosts unless the isolated external-service opt-in is set. Do not remove that protection to make a test pass.

The packaged desktop runtime initializes its local SQLite schema with
`Base.metadata.create_all` and an additive schema-repair pass. Focused
`unit_no_db` tests cover that repair using in-memory SQLite, including newly
added DeepSpace goal fields and indexes while preserving existing rows. Do not
point Alembic at a user's `~/.averqel-harness/db/knowledge.db`: desktop SQLite
databases are not Alembic-versioned, and replaying the full migration history
can collide with their existing tables.

Run the frontend checks separately:

```bash
pnpm --dir frontend lint
pnpm --dir frontend test
pnpm --dir frontend build
```

## Database-backed tests

Database-backed tests require separately provisioned, disposable PostgreSQL,
Redis, and MinIO services on loopback. The fixture validates the service
addresses and refuses databases not suffixed `_test`, Redis database 0, or
buckets without a `-test`/`_test` suffix. Set the test URLs documented in
[`isolated-test-runs.md`](../security/isolated-test-runs.md), then run from
`backend/`:

```bash
AVERQEL_TEST_ISOLATED_RUN=external pytest -m 'db and not e2e'
```

Coverage can be collected with:

```bash
AVERQEL_TEST_ISOLATED_RUN=external COVERAGE_FILE=/tmp/deepspace.coverage \
  pytest -m 'db and not e2e' --cov=app --cov-branch --cov-report=term-missing
```

Do not configure tests with active development, staging, or production
credentials or services. The checkout no longer provides container-based
deployment or test tooling. Database-free tests remain the normal local path.

The backend quality gates can be run from `backend/` with:

```bash
bash ../.github/scripts/run-backend-gate.sh ruff
bash ../.github/scripts/run-backend-gate.sh black
bash ../.github/scripts/run-backend-gate.sh mypy
bash ../.github/scripts/run-backend-gate.sh bandit
bash ../.github/scripts/run-backend-gate.sh pip-audit
bash ../.github/scripts/run-backend-gate.sh safety
bash ../.github/scripts/run-backend-gate.sh pytest
```

The mypy gate scans the application source tree and excludes generated build
outputs, Alembic revision files, and operational scripts. This keeps the
reported type baseline focused on maintained application code; generated
artifacts are covered by their build and smoke checks instead.

The `ruff-fix` and `black-format` gates modify files. Run them only when those edits are intended, then inspect the full diff.

## Pull-request CI

`.github/workflows/ci.yml` is the required pull-request gate for `main`. Its
`CI Passed` aggregate check requires all applicable jobs to succeed:

| Job | Coverage | Isolation |
| --- | --- | --- |
| Workflow lint | Actionlint and patch whitespace | GitHub-hosted runner |
| Backend quality | pre-commit, Ruff, Black, mypy, Bandit, blocking `pip-audit`, advisory Safety review, and `unit_no_db` | No application services or database |
| Frontend quality | lint, Vitest, production build, and production dependency audit | GitHub-hosted runner |
| Browser E2E | Full Playwright suite | Loopback-only temporary Next app and closed API sink |
| Desktop shell | Electron process and routing tests | GitHub-hosted runner |
| Desktop Linux packaged runtime | Frozen local API build and readiness/OpenAPI/SQLite smoke test | Temporary local home and loopback port |
| Dependency review | Changed dependency vulnerability review | Pull requests only |

Dependency Review requires the repository's GitHub Dependency graph to be
enabled. CI performs a read-only capability check: when the graph is enabled,
the action is blocking; when it is unavailable, the review job is explicitly
skipped with a warning while the pinned `pip-audit`, Safety, and frontend
dependency audits remain independent gates. Enable the graph under the
repository's Security analysis settings to activate the full changed-
dependency review.

Safety is a supplemental advisory audit and requires the `SAFETY_API_KEY`
repository secret. When the key is absent, the gate reports an explicit skip;
when configured, findings remain advisory. `pip-audit`, Bandit, and the
frontend production dependency audit remain blocking merge gates.

The workflow cancels superseded runs for the same pull request and grants jobs
read-only repository permissions. It never targets development, staging, or
production services. `CI Passed` is the only required branch-protection
context; the manual-only `Desktop artifacts` workflow remains the native
six-target packaging and artifact-upload workflow described below. It does not
run for ordinary branch pushes or pull requests.

Run the locally reproducible CI suite before pushing:

```bash
bash .github/scripts/run-local-pr-ci.sh
```

The installed Git pre-push hook runs the same suite against the exact outgoing
commit in an isolated worktree under `~/.cache/averqel-kinesis/pre-push-worktrees`
(or `$XDG_CACHE_HOME`). It requires the pinned Python 3.12 environment, desktop
pnpm dependencies, pre-commit, pnpm, Actionlint, and Playwright Chromium. The
hook installs frontend dependencies from the committed lockfile inside the
worktree, keeping Next.js package resolution inside its project root. The
home-filesystem worktree also avoids `/tmp` tmpfs limits during desktop package
building. The runner executes independent PR CI job groups in parallel and
keeps browser E2E loopback-only. It never installs operating-system packages
or targets a live application service. GitHub Dependency Review is hosted-only.
Separate nightly, native-platform, and artifact workflows are not PR checks and
remain available in GitHub Actions when their triggers or manual dispatch are
used. Tool-calling fixture replay is reported as skipped until all required
reviewed JSONL baselines are committed; a skip is not tool coverage evidence.

## Native process and terminal tests

The manual-only `DeepSpace native runtime tests` GitHub Actions workflow runs mocked Job
containment/subprocess tests and a real pywinpty input/output round trip on
Windows x64 and on an ARM64 Windows host using the supported x64 Python
runtime. The Win32 Job controller and Windows PTY host are gated at 100%
statement and branch coverage, and the focused jobs fail if any selected test
is skipped. Run the native Windows PTY test locally from `backend/` with:

```powershell
python -m pytest -o addopts="" platform-tests/windows/test_windows_terminal_pty.py
```

The Windows test starts the real contained PTY host, verifies interactive
input/output, waits for the child to exit, and checks that its Job Object is
released. The mocked Windows unit tests can also run on other operating
systems; they do not replace this native Windows check.

## Live provider tests

Live-provider tests are opt-in. Set `AVERQEL_LIVE_PROVIDER_TYPE`,
`AVERQEL_LIVE_BASE_URL`, and `AVERQEL_LIVE_MODEL`; the second model and API key
are optional. Run the focused tests directly with pytest, and use only a
disposable or explicitly approved provider endpoint. Keep credentials in the
environment.

## Native Office runtime test

This explicit integration test calls the real `office_process` tool. It needs
Python dependencies from `requirements.txt`, LibreOffice, and Poppler's
`pdftoppm`; all generated files stay under pytest's temporary directory:

```bash
sudo apt-get install -y libreoffice poppler-utils
cd backend
python -m pytest -o addopts="" platform-tests/office/test_real_office_runtime.py
```

The desktop artifact workflow runs this test on Linux x64. Each native Linux,
Windows, and macOS artifact runner also verifies the packaged runtime manifest
and runs its platform-relative manifest tests before the backend smoke test.
The verifier command is `averqel-backend --verify-runtime-manifest`; it exits
without starting the API server.

The packaged backend smoke test also exercises SQLite startup on each native
runner. Python builds can omit optional `sqlite3.Connection` methods such as
`enable_load_extension`; SQLite connection setup must keep working when those
optional APIs are unavailable.

## Before reporting a result

Record the exact command and whether it passed, failed, or was skipped. A database-free pass does not verify database migrations, external integrations, or browser workflows. Never describe a partial run as the full suite.

Related: [isolated test runs](../security/isolated-test-runs.md), [tenant scope](01-tenant-scope.md), and the [DeepSpace manual](../deepspace/README.md).

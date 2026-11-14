# Testing the backend safely

Use database-free tests for normal code changes. Database-backed and live-provider tests run only against isolated, disposable services.

## Database-free tests

From the repository root, run:

```bash
cd backend
pytest -m unit_no_db
```

`unit_no_db` tests use fake or in-memory dependencies. The test bootstrap replaces inherited database, Redis, and object-store URLs with unreachable test-only hosts unless the isolated external-service opt-in is set. Do not remove that protection to make a test pass.

Run the frontend checks separately:

```bash
pnpm --dir frontend lint
pnpm --dir frontend test
pnpm --dir frontend build
```

## Database-backed tests

The normal database-backed test path creates a uniquely named Docker Compose project. From the repository root:

```bash
./backend/scripts/test-isolated.sh
```

Pass pytest arguments after the script to narrow a run, for example:

```bash
./backend/scripts/test-isolated.sh -m 'db and not e2e'
```

To measure statement and branch coverage for the complete backend suite, run:

```bash
COVERAGE_FILE=/tmp/deepspace.coverage AKS_TEST_XDIST_MAX_WORKERS=4 \
  ./backend/scripts/test-isolated.sh --cov=app --cov-branch --cov-report=term-missing
```

Coverage data is written under the runner's temporary filesystem because the
source tree is mounted read-only. The report is printed by pytest when the run
finishes.

The test Compose stack gives PostgreSQL, Redis, MinIO, and the test runner a private network namespace. The runner executes as the unprivileged `appuser`, so filesystem-permission tests behave like they do for a normal desktop process. Its test-only image includes Bash, Git, OpenSSH, Node.js, Chromium, and bubblewrap so tests do not silently skip merely because those executables are absent. The runner still drops all Linux capabilities and disallows privilege gain; cage tests that require nested user/PID namespaces may therefore report an explicit skip. Live-provider tests also remain opt-in and require a configured disposable provider endpoint. Service data lives on temporary filesystems, the source tree is mounted read-only, and the script removes that test project and its volumes when it exits. Docker must be available. Do not connect these tests to the regular application Compose stack or to a development, staging, or production database.

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

The `ruff-fix` and `black-format` gates modify files. Run them only when those edits are intended, then inspect the full diff.

## Pull-request CI

`.github/workflows/ci.yml` is the required pull-request gate for `main`. Its
`CI Passed` aggregate check requires all applicable jobs to succeed:

| Job | Coverage | Isolation |
| --- | --- | --- |
| Workflow lint | Actionlint and patch whitespace | GitHub-hosted runner |
| Backend quality | pre-commit, Ruff, Black, mypy, Bandit, Python dependency audits, and `unit_no_db` | No application services or database |
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

The workflow cancels superseded runs for the same pull request and grants jobs
read-only repository permissions. It never targets development, staging, or
production services. `CI Passed` is the only required branch-protection
context; the manual `Desktop artifacts` workflow remains the native six-target
packaging and artifact-upload workflow described below.

## Native process and terminal tests

The `DeepSpace native runtime tests` GitHub Actions workflow runs mocked Job
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

`./backend/scripts/test-live.sh` runs configured providers through the product's provider registry. Set `AVERQEL_LIVE_PROVIDER_TYPE`, `AVERQEL_LIVE_BASE_URL`, and `AVERQEL_LIVE_MODEL`; `AVERQEL_LIVE_SECOND_MODEL` is optional and defaults to the primary model. `AVERQEL_LIVE_API_KEY` is optional for local or unauthenticated providers. For example, a Docker-reachable LM Studio server can use provider `lmstudio`, endpoint `http://host.docker.internal:1234/v1`, and a loaded model ID. The server must accept connections from the Docker host gateway; `127.0.0.1` inside the test container refers to the container itself. The live runner enables provider network access, so point it only at a disposable or explicitly approved endpoint and keep credentials in the environment.

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

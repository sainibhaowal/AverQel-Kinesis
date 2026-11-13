# Isolated test runs

Tests that can reset a database, flush Redis, write object storage, or send network requests must use disposable targets. Never point a test at an active development, staging, or production service.

## Database, Redis, and object storage

Prefer the isolated Docker runner from the repository root:

```bash
./backend/scripts/test-isolated.sh
```

It creates a unique Compose project, starts PostgreSQL, Redis, MinIO, and the test runner in an isolated network namespace, uses temporary filesystems for service data, mounts the repository read-only, and removes the test project and its volumes on exit.

If an integration needs services provisioned outside that runner, use only dedicated disposable services bound to loopback. Set `AVERQEL_TEST_ISOLATED_RUN=external` and the `AKS_DATABASE_URL`, `AKS_REDIS_URL`, and `AKS_MINIO_*` test settings. The test bootstrap rejects configurations unless:

- PostgreSQL is on `127.0.0.1:5432` and its database name ends in `_test`;
- Redis is on `127.0.0.1:6379` and uses a nonzero database number;
- MinIO is on `127.0.0.1:9000` and its bucket ends in `-test` or `_test`.

Example, after you have provisioned those dedicated services:

```bash
cd backend
AVERQEL_TEST_ISOLATED_RUN=external pytest -m 'db and not e2e'
```

The opt-in does not create services or make an unsafe target safe. Confirm every URL, database name, Redis database, bucket, and credential before running the command. Never use a production or shared credential.

## Browser end-to-end tests

The default Playwright run starts its own local server on `127.0.0.1:3103`, refuses to reuse an existing server, and sends API requests to the closed local test sink:

```bash
pnpm --dir frontend e2e
```

External browser targets are disabled by default. Use them only against an explicitly disposable target, and set both the target URL and the opt-in:

```bash
DEEPSPACE_E2E_BASE_URL=https://disposable-test.example \
DEEPSPACE_E2E_ALLOW_EXTERNAL=1 \
pnpm --dir frontend e2e
```

Set `DEEPSPACE_E2E_STORAGE_STATE` or `DEEPSPACE_E2E_AUTH_TOKEN` only when that disposable target requires authentication. Keep those values out of source control and logs. Do not point browser tests at a live user account or production site.

## Live provider tests

`./backend/scripts/test-live.sh` requires `AVERQEL_LIVE_PROVIDER_TYPE`, `AVERQEL_LIVE_BASE_URL`, and `AVERQEL_LIVE_MODEL`. `AVERQEL_LIVE_API_KEY` is optional for local or unauthenticated providers; `AVERQEL_LIVE_SECOND_MODEL` is optional and defaults to the primary model. The test runner enables provider network access. Use a disposable or explicitly approved endpoint and never use production credentials or data. A local service in Docker tests must be reachable through the host gateway; a host-only `127.0.0.1` listener is not reachable from the test container.

Related: [backend testing](../platform/03-testing.md) and [tenant scope](../platform/01-tenant-scope.md).

# AverQel contributor and release quick reference

This page is a short command and workflow guide. It follows the repository's
current protected-branch, manual-release, and manual-deployment setup. The
canonical contributor rules are in [`CONTRIBUTING.md`](CONTRIBUTING.md); the
release and deployment details are in [`README.md`](README.md) and the linked
GitHub Actions workflows.

## Work on a change

Start from an up-to-date checkout, create a branch, make the change, and run
the relevant checks:

```bash
git switch main
git pull --ff-only origin main
git switch -c docs/short-description

# Run checks that match the files you changed.
pnpm --dir frontend lint
pnpm --dir frontend test
pnpm --dir frontend exec tsc --noEmit
pnpm --dir frontend build
```

For backend changes, use the project virtual environment from `backend/`:

```bash
cd backend
./.venv/bin/ruff check app tests
./.venv/bin/pytest -q
```

Do not run formatting commands that rewrite unrelated files. Review
`git status --short` and the diff, then commit only the intended files:

```bash
git status --short
git diff --check
git diff
git add path/to/changed-file
git commit -m "docs: explain a product workflow"
git push -u origin docs/short-description
```

Open a pull request and wait for required review and CI. Do not push directly
to protected `main`.

## Publish a desktop release

The **Release - Manual SemVer and Desktop** workflow is run manually from
protected `main`. It selects or calculates a canonical `vMAJOR.MINOR.PATCH`
version, builds desktop packages, and publishes the GitHub release. It does
not deploy the web application to the VPS.

Use GitHub **Actions → Release - Manual SemVer and Desktop → Run workflow**.
Leave the version input empty to calculate the next version, or enter an exact
canonical version when the release process calls for one. Review the generated
release assets and manifest after the workflow completes.

## Local desktop product

AverQel Kinesis is shipped as a locally installed Electron application. This
repository does not provide a Docker/VPS deployment workflow. Build and
artifact checks are run through the desktop GitHub Actions workflow.

## Desktop development

The desktop client uses the shared frontend and normally opens the local web
app with:

```bash
node scripts/electron.mjs dev
```

To explicitly use the local HTTPS reverse proxy and its API, set both values:

```bash
ELECTRON_START_URL=https://averqel.localhost \
NEXT_PUBLIC_API_URL=https://averqel.localhost/api/v1 \
node scripts/electron.mjs dev
```

Desktop packaging instructions are in
[`applications/desktop/README.md`](applications/desktop/README.md).

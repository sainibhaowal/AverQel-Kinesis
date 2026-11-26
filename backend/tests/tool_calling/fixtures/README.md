# Tool-calling fixture baselines

Local fixture replay expects recorded JSONL baselines in this directory. No
baselines are currently committed, so replay and tool-coverage checks cannot
run until they are recorded and reviewed. There is currently no dedicated
GitHub Actions workflow for these checks.

Use the recording instructions in [`../README.md`](../README.md) with an
explicitly disposable local test setup. Review the traces for secrets and
machine-specific data before committing them. Do not add empty placeholder
JSONL files: they would appear to configure replay without providing test
coverage.

# Tool-calling fixture baselines

The replay workflow expects recorded JSONL baselines in this directory. No
baselines are currently committed, so replay and tool-coverage checks cannot
run until they are recorded and reviewed.

Use the recording instructions in [`../README.md`](../README.md) with an
explicitly disposable local test setup. Review the traces for secrets and
machine-specific data before committing them. Do not add empty placeholder
JSONL files: they would make the workflow appear configured without providing
test coverage.

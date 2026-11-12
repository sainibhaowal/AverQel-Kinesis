# 50. Lazy optional imports

## Use case

The API should not load heavyweight or optional runtime packages until a
feature actually needs them, and a failed optional import should be retryable
if the environment is repaired during the process lifetime.

## Why it matters

Eager imports slow startup and make unrelated features fail when an optional
dependency is unavailable. Caching a failed import would keep a repaired
runtime unusable until restart.

## How it works

`app/core/lazy_import.py` provides `lazy_import(module_name, package=None)`.
It resolves absolute and package-relative names with Python's import
machinery, uses a lock to avoid duplicate concurrent loads, and caches only
successful module objects. Import exceptions propagate unchanged and are not
cached, so the next call can retry.

DeepSpace uses the helper at optional provider HTTPX entry points, URL/browser
readers, the browser WebSocket client, and local `sentence_transformers`
inference. This preserves the existing request, sandbox, and SSRF behavior; it
changes when the module is imported, not what the feature is allowed to do.

## What to watch for

- The helper applies only to call sites wired to it. A normal top-level Python
  import elsewhere remains eager.
- Successful modules follow Python's process-wide module cache; clearing the
  helper's small reference cache is intended for isolated tests, not runtime
  dependency hot-swapping.
- Dynamic plugin loading intentionally keeps its existing import path so
  plugin reload and hot-module-reload behavior is not hidden behind the
  optional-dependency cache.
- Focused tests are in `backend/tests/unit/test_lazy_import.py`.

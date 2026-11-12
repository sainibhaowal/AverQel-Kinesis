# 49. Outbound HTTP proxy policy

## Use case

A user or operator needs provider HTTP requests and the product-owned Codex
CLI process to reach the network through a configured proxy, while local and
explicitly isolated requests stay direct.

## Why it matters

Different clients can otherwise interpret proxy variables differently, expose
credentials in diagnostics, or accidentally send loopback and SSRF-checked
requests through an external proxy.

## How it works

`app/core/http_proxy.py` validates `HTTP_PROXY`, `HTTPS_PROXY`, and
`ALL_PROXY` (and their lowercase spellings). A scheme-specific value wins;
`ALL_PROXY` is the fallback. Only HTTP and HTTPS proxy URLs with a host and
valid port are accepted. Invalid values are ignored and diagnostics include
only variable names and configuration-presence flags, never proxy URLs or
credentials.

`NO_PROXY`/`no_proxy` supports exact hosts, domain suffixes, optional ports,
wildcards, and IP networks; localhost and loopback addresses are always
bypassed. At backend startup the validated policy is normalized into the
process environment for ordinary HTTPX clients that use `trust_env=True`, and
the original values are restored on orderly shutdown. The trusted Codex CLI
child gets a fresh, normalized proxy overlay after the existing credential
filter.

SSRF-protected URL readers, browser-local health checks, MCP clients, and
sandboxed shell/PTC/browser processes that explicitly use `trust_env=False` or
a reduced environment remain outside this proxy route. Browser automation's
separate explicit proxy setting is unchanged.

## What to watch for

- Proxy authentication stays in process memory and child environment only;
  never include it in logs, errors, or model-visible diagnostics.
- Proxy settings do not override `trust_env=False`, local-only, SSRF, or
  sandbox network controls.
- Policy matching is unit-tested, but real corporate proxies, authentication
  schemes, and every HTTPX client/provider need host-specific acceptance tests.
- Focused tests are in `backend/tests/unit/test_http_proxy_policy.py` and
  `backend/tests/unit/test_codex_cli_runtime.py`.

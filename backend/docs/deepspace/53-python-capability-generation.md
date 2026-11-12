# 53. Python capability transport and generation

This chapter explains authenticated Typert transports and the Python-native
artifact generator.

## Use case

Authenticated clients need to call explicitly published capabilities across a
process boundary. Package authors and maintainers also need generated clients
and schemas that match the capability registry mounted by DeepSpace.

## Why it matters

Handwritten client signatures and manifests drift away from runtime endpoint
contracts. A deterministic generator makes those differences visible. The
authenticated transports let eligible Python packages use the same registry
contract outside the process that mounted it.

## How it works

Authenticated unary calls use `POST /api/v1/deepspace/typert/invoke` with
`{"endpoint":"package/method","arguments":{...}}`. The normal bearer-token
or API-key authentication and `queries:run` permission apply. In addition,
the capability must declare a `scope` matching one of the caller's granted
permissions. Endpoints without a scope are denied. Calls declaring
identity-bound resources are checked against a resource-kind ownership adapter
before dispatch. The built-in adapters scope conversations, workspaces, Library
files, documents, terminals, SSH hosts, media artifacts, workflow jobs, and
schedules to the authenticated tenant and user. Deployment-installed plugins
are shared application resources; workspace plugins are checked against the
authenticated account's registered workspace roots. Unknown plugins and
resource kinds without an authoritative owner adapter remain denied. A
declared `global` resource needs no row-level ownership check.

Duplex stream calls use `WS /api/v1/deepspace/typert/invoke/stream`. The client
authenticates with an Authorization bearer header, then sends an initial
`{endpoint, arguments}` object. The server
sends `ready`, zero or more `downlink` values, and `complete`; the client sends
`uplink` values and ends its direction with `end`. A client can send `cancel`
or close the socket to dispose both directions. Closing a stream cancels its
invocation and releases invocation-owned resources.

The live registry renderer produces, per package, a JSON manifest, a Python
client module that calls `TypertRemoteClient`, and one JSON Schema file per
contributed schema. Run it from
any working directory with:

```bash
python backend/scripts/deepspace_typert_codegen.py --output /path/to/generated
```

Use `--package PACKAGE` one or more times to restrict generation, and
`--face host` or `--face client` to select the mounted face. The command
preserves unrelated files and replaces each generated file atomically. It does
not delete stale files; remove obsolete artifacts only after reviewing the
generator's package list.

To verify that an existing artifact tree matches the live registry without
writing files:

```bash
python backend/scripts/deepspace_typert_codegen.py --output /path/to/generated --check
```

The program exits with status 1 and lists missing or changed artifact paths if
the tree is stale. Its output paths reject traversal and symlink escapes.

## What to watch for

This is the Python equivalent of the Harness build generator: it produces
Python clients and JSON artifacts. It does not emit TypeScript, Zod, `.d.ts`,
or tsdown build files because this runtime is Python-native. Generation only
includes packages mounted in that process, so run it with the same configured
package discovery settings used by the target build.

Resource authorization is fail-closed: a missing database session, malformed
identity, missing adapter, database error, or owner mismatch denies the call.
The adapter checks run for both unary calls and WebSocket streams before the
capability is invoked. Adding a new resource kind to the registry does not
grant it remote access; its owner store must be connected explicitly.

Focused authentication and ownership tests pass with **100% statement and
branch coverage** for `resource_authorization.py`; this is coverage of that
adapter module, not every Typert transport or generator module.

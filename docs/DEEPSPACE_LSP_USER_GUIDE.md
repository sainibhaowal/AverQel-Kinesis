# DeepSpace LSP (Language Server Protocol) — User Guide

**Date:** 2026-11-19

---

## What It Is

**LSP** gives the agent precise code navigation through configured language servers: go to definition, find references, go to implementation, and hover documentation. It works where text search fails — distinguishing same-named functions, following imports, connecting interfaces to implementations, and reading inferred types.

**Use it when:**
- Textual search is ambiguous (multiple `foo` functions across files)
- You need precise symbol relationships before a change
- The agent should "understand" code structure, not just text

**Skip it when:**
- Ordinary `search`/`read` answers the question
- No language server is configured for the file type

---

## Where to Configure

Add servers through the settings API (`deepspace_lsp_servers` dict). There is no UI form yet.

```json
{
  "deepspace_lsp_servers": {
    "typescript": {
      "command": ["typescript-language-server", "--stdio"],
      "extension_to_language": { ".ts": "typescript", ".tsx": "typescript" }
    },
    "python": {
      "command": ["pylsp"],
      "extension_to_language": { ".py": "python" }
    }
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `command` | Yes | Executable + args (launched without shell) |
| `extension_to_language` | Yes | Lowercase leading-dot extension → LSP language ID |
| `env` | No | Extra environment variables (secrets auto-scrubbed) |
| `initialization_options` | No | Static `initialize` options forwarded to server |
| `configuration` | No | Static `workspace/configuration` response |

**Deployments must supply and configure their language servers** — DeepSpace ships none.

---

## The Four Operations

| Operation | What the Agent Gets |
|-----------|---------------------|
| `goToDefinition` | The declaration site(s) of the symbol at the cursor |
| `findReferences` | Every reference, **always including the declaration** |
| `goToImplementation` | The concrete implementation site(s) |
| `hover` | Normalized documentation for the symbol, or none |

**Positions:** One-based UTF-16 line/character from the model; converted to zero-based on the wire.

**`findReferences` always includes declarations** — impact analysis never misses the defining site.

---

## How to Use (Natural Language)

| You Say | Agent Does |
|---------|------------|
| *"Go to definition of `foo` in `src/main.ts` line 10"* | `lsp(goToDefinition, "src/main.ts", 10, 5)` |
| *"Find all references to `UserService`"* | `lsp(findReferences, "src/user.ts", 5, 12)` |
| *"Hover over `calculateTotal` in `cart.py`"* | `lsp(hover, "cart.py", 20, 8)` |
| *"Show implementations of `IRepository`"* | `lsp(goToImplementation, "repo.ts", 3, 15)` |

The agent prefers `search`/`read` for ordinary navigation. It uses `lsp` when textual matches are ambiguous or before a change requiring precise definitions.

---

## What You See in the UI

| Location | What It Shows |
|----------|---------------|
| **Chat** | LSP tool call, status, duration, locations/hover result |
| **Tool result** | File-grouped `path:line:char` locations or normalized hover text |
| **Settings → DeepSpace → Runtime** | Configured servers, their extensions, and status |

**Result caps:** `maxLocations` (default 100) bounds navigation items; `maxResultChars` (default 16,000) bounds complete rendered result. Omission/truncation markers appear inside the cap.

---

## Error Codes

| Code | Meaning | What to Do |
|------|---------|------------|
| `No LSP provider registered for extension '.py'` | No server configured for that file type | Add a provider in `deepspace_lsp_servers` for the extension |
| `Operation goToDefinition not supported by this server` | The language server lacks the capability | Use a server that supports the operation |
| `LSP server not running` | Process failed to start | Check `command` is on PATH and executable |
| `LSP request textDocument/definition timed out` | Server did not respond in 30s | Increase `deepspace_lsp_tool_timeout_ms` or check server health |
| `LSP server not initialized` | Initialize handshake failed | Check server logs; try a simpler server |

The tool returns `{"status": "error", "error": "..."}` for all failure modes. There are no distinct `LSP_UNAVAILABLE`, `LSP_UNSUPPORTED_OPERATION`, etc. codes — the message describes the cause.

---

## Limits & Failure Modes

| Failure | Behavior |
|---------|----------|
| No provider for extension | Error returned with message |
| Server doesn't support operation | Error returned with message |
| Source missing/oversized/non-UTF-8 | Error from file read before server starts |
| Server crashed | Auto-retries once on fresh connection; then error |
| No workspace selected | Workspace is resolved from conversation; no separate concept |

**Per-workspace serialization:** Queries to the same server+workspace run one at a time; different workspaces run in parallel. A transport failure retries the read-only query once on a fresh process.

---

## Deployment Settings

| Setting | Default | Effect |
|---------|---------|--------|
| `deepspace_lsp_enabled` | `false` | Master toggle (when false, no servers are registered) |
| `deepspace_lsp_servers` | `{}` | Server configurations (see above) |
| `deepspace_lsp_tool_timeout_ms` | `60000` | Tool-call timeout (covers open/query/close lifecycle) |
| `deepspace_lsp_max_locations` | `100` | Omission cap for navigation results |
| `deepspace_lsp_max_result_chars` | `16000` | Truncation cap for complete rendered result |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No LSP provider registered for extension '.ts'` | No server for `.ts` | Add `typescript-language-server` config |
| `Operation hover not supported by this server` | Server lacks `hoverProvider` | Use a server that supports hover |
| Empty results | Off-symbol cursor position | Move cursor onto the symbol |
| Server won't start | `command` not found / not executable | Use absolute path or ensure on PATH |
| Timeout | `deepspace_lsp_tool_timeout_ms` too low | Increase for slow servers |

---

## Related Documentation

| Topic | Document |
|-------|----------|
| Tool catalogue | `backend/docs/deepspace/11-deepspace-native-tool-surface.md` |
| LSP extension map generation | `backend/docs/deepspace/39-native-utilities.md` |

---

## TL;DR

- Configure language servers in `deepspace_lsp_servers` with `command` and `extension_to_language`
- Four operations: `goToDefinition`, `findReferences`, `goToImplementation`, `hover`
- Positions are one-based UTF-16; the tool converts
- `findReferences` always includes declarations
- Errors return a descriptive message, not a fixed code enum
- No language servers ship with Kinesis — you provide them
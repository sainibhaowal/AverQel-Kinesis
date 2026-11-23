# DeepSpace MCP (Model Context Protocol) — User Guide

**Date:** 2026-11-19

---

## What It Is

**MCP** lets the agent call tools and read resources from external Model Context Protocol servers as if they were native DeepSpace tools. Configure a server once; its tools appear in the agent's tool list with collision-safe names.

**Use it when:**
- You need GitHub, GitLab, Jira, Slack, database, or filesystem access via standard MCP servers
- The agent should act on external systems (create issues, query databases, post messages)
- You want a uniform interface across many external services

**Skip it when:**
- Native DeepSpace tools already cover the task
- The external service has no MCP server

---

## Where to Configure

Use the **MCP** page at `/dashboard/mcp` in the app. It lists connected servers, marketplace entries, health, and the inspector. The page creates custom servers through the API below.

**Streamable HTTP (remote service):**

```bash
curl -X POST http://localhost:10000/api/v1/mcp/servers/custom \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "web-search",
    "transport": "streamable_http",
    "server_url": "https://example.com/mcp",
    "auth_token": "your_token",
    "headers": {"X-Client": "kinesis"}
  }'
```

**stdio (local process):**

```bash
curl -X POST http://localhost:10000/api/v1/mcp/servers/custom \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "github",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "confirm_local_launch": true
  }'
```

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Display name (1–160 characters) |
| `transport` | No | `streamable_http` (default), `sse`, or `stdio` |
| `server_url` | HTTP/SSE | Endpoint URL; must start with `http://` or `https://` |
| `command` / `args` | stdio | Local executable and arguments |
| `confirm_local_launch` | stdio | Must be `true`; the call is refused with 428 otherwise |
| `auth_token` / `auth_header` | No | Bearer-style credential and header name (default `Authorization`) |
| `headers` | No | Extra request headers |
| `description` / `categories` | No | Catalog metadata |

The server is stored with status `connected` when its provider is available, and its tool catalog is refreshed through `POST /api/v1/mcp/servers/{server_id}/refresh`.

Other server endpoints: `GET /api/v1/mcp/servers`, `GET/PATCH/DELETE /api/v1/mcp/servers/{server_id}`, `POST .../toggle`, `GET .../tools`, `GET/PUT .../policy`, `GET .../inspector`, and the OAuth routes.

---

## Connect to a Conversation

```bash
# List the server connections and their per-conversation override
GET /api/v1/mcp/conversations/{conversation_id}/connections

# Turn one server on or off for the conversation
PUT /api/v1/mcp/conversations/{conversation_id}/connections/{server_id}
{"enabled": true}
```

`enabled: false` records an explicit switch-off for that conversation. A server is otherwise available when it is enabled, `connected`, the provider is available, and its connection policy has `default_enabled: true`.

There is a parallel DeepSpace-scoped route under `/api/v1/mcp/deepspaces/{deepspace_id}/connections`.

---

## Tool Naming & Discovery

Tool names are built from a short server id and the raw MCP tool name:

```
mcp_<first 10 hex chars of server id>_<raw tool name>
```

For example: `mcp_3f2a91c0b7_create_issue`. The name is truncated to 64 characters. Two servers can publish the same raw tool name without colliding because the server prefix differs.

The catalog refreshes on `POST /api/v1/mcp/servers/{server_id}/refresh`; a stale catalog is served from the last known tool list while a worker refresh is requested.

---

## How to Use (Natural Language)

| You Say | Agent Does |
|---|---|
| *"Create a GitHub issue in my repo"* | Calls the server-qualified MCP tool |
| *"List my Jira tickets"* | Calls the Jira search tool |
| *"Query the database for recent orders"* | Calls the database query tool |
| *"What MCP tools are available?"* | Lists the connected server tools |

The agent sees each tool with its exposed name, description, risk labels, and input schema.

---

## MCP Resources (Documents)

Three tools read resources from connected servers:

| Tool | Arguments | What It Does |
|---|---|---|
| `list_mcp_resources` | none | Lists resources (URI, name, MIME type) |
| `list_mcp_resource_templates` | none | Lists parameterized URI templates |
| `read_mcp_resource` | `uri` | Reads one resource; the owning server is chosen automatically |

Binary resources are returned as base64 in the tool result; text resources are returned as text. Resource reads respect an explicit per-conversation switch-off.

---

## What You See in the UI

| Location | What It Shows |
|---|---|
| **`/dashboard/mcp`** | Servers, marketplace, health, inspector, connect/disconnect |
| **Chat** | MCP tool calls with exposed names, arguments, and results |
| **System prompt** | The connected resource servers available to the turn |

---

## Connection Policy

Each server has a per-tenant/user policy (`GET/PUT /api/v1/mcp/servers/{server_id}/policy`):

| Field | Default | Effect |
|---|---|---|
| `default_enabled` | `false` | Whether the server's tools are exposed at all |
| `allowed_tools` / `denied_tools` | empty | Tool-level allow/deny lists |
| `read_only` | `true` | Blocks every non-read risk level |
| `risk_ceiling` | `read` | Highest allowed risk: `read`, `write`, `delete`, `external_message` |
| `approval_rules` | empty | Per-risk mode: `always_allow`, `needs_approval`, `blocked` |
| `tool_modes` | empty | Per-tool override of the approval mode |
| `default_tool_mode` | `needs_approval` | Mode for tools with no specific rule |
| `deepspace_overrides` / `conversation_overrides` | empty | Per-scope on/off map |

A call is refused when ownership, enabled state, connection status, provider availability, catalog revision, allow/deny, risk ceiling, read-only, or approval policy fails.

Approval is evaluated by the same runtime policy as native tools; when a call needs approval, the turn pauses and the approval card resumes it.

---

## Limits & Failure Modes

| Failure | Behavior |
|---|---|
| Server disabled or disconnected | Its tools are not exposed |
| Provider unavailable | Its tools are not exposed |
| Stale catalog | Last known catalog stays usable; a refresh is requested |
| Server crashes mid-call | The call fails; the other servers keep working |
| Catalog revision changed | The call is refused and asks for a tool-list refresh |
| Binary resource | Base64 payload in the tool result |
| MCP `isError` result | The call fails before any artifact is persisted |

Deployment limits: up to 50 servers per conversation and 100 tools per server. The bridge also budgets search results, schema size, result size, and calls per turn through the `deepspace_mcp_*` settings.

---

## Deployment Settings

| Setting | Default | Effect |
|---|---|---|
| `mcp_catalog_max_age_seconds` | `900` | Age after which the catalog is refreshed |
| `deepspace_mcp_max_search_results` | `5` | Tool-search result cap |
| `deepspace_mcp_max_schema_chars` | `6000` | Schema characters per tool |
| `deepspace_mcp_max_result_chars` | `12000` | Result characters per call |
| `deepspace_mcp_max_calls_per_turn` | `8` | MCP calls per turn |
| `deepspace_mcp_max_result_chars_per_turn` | `60000` | Result budget per turn |

There is no `deepspace_mcp_enabled` master switch: MCP availability follows the server connections and their policies.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Tool not in list | Server is disabled, disconnected, or `default_enabled` is false | Check the server and its policy |
| `MCP connection is disabled or disconnected` | Server status is not `connected` | Refresh or reconnect the server |
| `MCP connection policy is not configured` | No policy row | `PUT /mcp/servers/{id}/policy` with `default_enabled: true` |
| `MCP tool exceeds the connection risk ceiling` | Risk ceiling is lower than the tool's risk | Raise the ceiling deliberately |
| `MCP tool is blocked by read-only mode` | `read_only: true` | Turn it off only for trusted servers |
| `MCP tool catalog is stale` | Catalog older than the max age | Refresh the server |
| `confirm_local_launch` error | stdio server added without confirmation | Send `confirm_local_launch: true` |
| Tool call timeout | Server is slow | Raise the server's timeout or check its health |

---

## Related Guides

- `backend/docs/deepspace/18-mcp-and-connectors.md` — MCP chapter
- `backend/docs/deepspace/11-deepspace-native-tool-surface.md` — MCP tool reference
- [DeepSpace Permission Guide](DEEPSPACE_PERMISSION_USER_GUIDE.md) — Sandbox and approval
- [DeepSpace Settings Forms Guide](DEEPSPACE_SETTINGS_FORMS_USER_GUIDE.md) — Deployment settings

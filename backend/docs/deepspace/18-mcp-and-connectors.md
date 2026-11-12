# 18. MCP and connectors

The MCP bridge is the way a DeepSpace conversation reaches the servers it is
connected to. A connector is a different thing: a scheduled feed that brings
an outside system's documents into the product. This chapter describes the
bridge, the servers it reaches, the tools it offers, the resources it reads,
and the limits on all of it.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [The connected servers](#the-connected-servers)
  - [The connection policy](#the-connection-policy)
  - [The broker](#the-broker)
  - [The tool side](#the-tool-side)
  - [The resource side](#the-resource-side)
  - [A server that publishes no resources](#a-server-that-publishes-no-resources)
  - [The limits](#the-limits)
  - [The routes](#the-routes)
  - [Connectors](#connectors)
- [What to watch for](#what-to-watch-for)

## Use case

- The user connects their issue tracker to AverQel and asks the agent to
  file an issue.
- The user connects a company knowledge base and asks the agent to answer
  from it.
- The agent needs to read a document the server publishes as a resource
  rather than as a tool result.
- An operator wants to know which connected servers a deployment may use and
  which tools it blocks.

## Why it matters

- The complete catalogue of a connected service is large, and sending all of
  it to the model on every round fills the context window.
- A model that names a server and a raw tool name can call a tool the
  deployment never offered.
- A connected account carries a credential, so the server that owns it must
  be checked on every call, not only when it is connected.
- A server that publishes tools but no resources is normal, and it must not
  look like a failure.

## How it works

The bridge is narrow on purpose. It owns no OAuth logic and no transport
logic. Connection ownership, catalogue freshness, policy, and remote
execution stay in the MCP integration service, and the bridge discovers
tools from the user's connected accounts and forwards approved calls through
that service.

### The connected servers

A server is in reach for a conversation when all of the following hold.

- It belongs to the authenticated tenant and user.
- It is enabled and its status is `connected`.
- Its provider is still approved.
- Its connection policy exists and has `default_enabled` set.

At most 50 servers are read, newest first, and at most 100 tools are taken
from each server's cached catalogue.

The catalogue comes from the server's own cached tool list. When that list is
older than `mcp_catalog_max_age_seconds`, which is 900 seconds by default,
the last known list stays available and a refresh is asked for in the
background. A connected server therefore does not disappear from the model
because its cache aged out, and the execution policy still checks ownership,
connection, risk, and approval before every call.

Each tool gets an exposed name made of `mcp_`, the first ten hexadecimal
characters of the server id, an underscore, and the tool name, at most 64
characters long. The prefix keeps two providers that publish the same tool
name apart.

### Connecting a local STDIO server from chat

When a request explicitly needs MCP and no matching provider is connected,
DeepSpace can pause that request and show matching, AverQel-approved local
STDIO providers. An unrelated connected MCP does not satisfy a request for a
named service. Failed or pending local connections can be retried from the
same chat card. The card shows the exact executable and argument list,
and explains that the process runs on the host where the MCP worker executes,
under that worker's operating-system account. In the desktop app this is the
local desktop account. The user must choose **Connect** before launch;
**Cancel** resumes the same request without MCP.

The STDIO process can use files and network resources available to the worker's
operating-system account and is not wrapped by DeepSpace's shell sandbox. The
prompt is therefore a real host-process consent step, not a claim that the
command is confined to the workspace. STDIO launch confirmation only authorizes
starting that provider. It does not approve any later remote write, delete, or
external message: the connection policy and normal per-tool approval checks
still apply. In a distributed deployment, the worker host is the machine that
launches the process; this connection does not remotely launch it on the user's
desktop.

After Connect, the connection remains `connecting` and is not offered to the
model until the background worker has started it and loaded its live catalog.
Only then does the worker mark it `connected`. If queueing, process startup, or
catalog loading fails, the connection is `failed`, no MCP tools are exposed,
and the chat keeps the request paused so the user can retry or cancel. Preview
tool metadata is not used as a live catalog for local STDIO providers.

The chat asks to connect only when the user explicitly requests an MCP action
or names a provider that is needed for the task. A conditional safety note such
as “if you need MCP, show me what will run first” is not by itself a request to
use MCP; ordinary searches stay on the selected workspace tools. In a paused
connection card, **Cancel and continue without MCP** resumes the requested task
without connecting, while **Stop this task** ends the paused run.

### The connection policy

The policy is a durable record per server, and it is checked before every
remote call, not only when the server is connected.

| Field | What it holds |
|---|---|
| `allowed_tools` | The tool names the connection permits. Empty means every tool the server publishes is allowed. |
| `denied_tools` | The tool names the connection blocks. These always win. |
| `read_only` | When true, only a read-risk tool may run. |
| `risk_ceiling` | The highest risk the connection allows: `read`, `write`, `delete`, or `external_message`. |
| `default_tool_mode` | `always_allow`, `needs_approval`, or `blocked`. |
| `tool_modes` | The mode of each named tool. |
| `approval_rules` | The mode per risk level. |
| `default_enabled` | Whether the connection is on at all. |
| `conversation_overrides` | Per-conversation switches. A value of `false` keeps that server out of that conversation. |

A call is refused when the tool is denied, when the tool is not in the
allowlist, when the tool's risk is above the ceiling, when a read-only
connection would run a write, when the tool's mode is `blocked`, or when the
catalogue revision moved since the model saw it.

New standard MCP connections start read-only with a `read` risk ceiling and
allow safe reads automatically. Routine write-risk actions can run
automatically only when the user enables writes and the applicable per-tool or
risk-level policy says `always_allow`. Deletes, outbound messages, sensitive
settings/security actions, and tools with no recognized action name or
reviewed risk label still require a human decision even when the master mode
says `always_allow`. Command-execution tools always require review. Unknown
tool names are treated as write-risk unless reviewed labels classify them more
precisely.
A risk-level `needs_approval` or `blocked` rule remains in force even when a
tool-specific mode is set to `always_allow`.
Existing saved connection policies are preserved; these defaults apply when a
new policy is created. Users can review or change an existing policy in the
connection inspector.
New Composio OAuth connections also enable routine write-risk actions within
their granted provider scopes. All MCP calls still pass ownership, connection,
allowlist, risk-ceiling, and catalog checks immediately before execution.

A tool whose effective mode is `needs_approval` does not run straight away.
The turn pauses with an approval request that names the server and the tool,
and the call runs once the user answers. The three broker metadata operations
never pause, because they change nothing on the server.

### The broker

The model never sees the complete catalogue. It sees one tool, `mcp_broker`,
with one argument, `operation`.

| Operation | What it does |
|---|---|
| `search` | Finds the connected tools that match a short description. |
| `schema` | Loads the compact input schema of one tool. |
| `call` | Runs exactly one tool. |
| `result` | Reads a stored result by its reference. |

The four operations behind the broker are `mcp_search_tools`,
`mcp_get_tool_schema`, `mcp_call_tool`, and `mcp_get_result`. The broker is
the only MCP tool the model is offered, and the complete catalogue and the
exact upstream schemas stay on the server.

A `search` returns compact references only. Each match carries a
`tool_ref`, the server name, the tool name, a short description, and the
names of the tool's parameters. The exact schema is not included.

A `schema` call loads one compact schema for one `tool_ref`. The exact
private schema stays server-side and is used to validate the call.

A `call` runs one tool. The model supplies the `tool_ref` it got from a
search and the arguments for that tool. The model cannot name a server, a
transport, or a raw tool name, because a reference that was not returned by
this request's own search resolves to nothing.

A `result` call reads a stored result back by its reference. After a remote
call the full result is kept in a short-lived store under the tenant, the
user, and the conversation, and the model receives a preview and an opaque
reference. A reference that has expired answers "The MCP result reference is
missing or expired."

A request to find or inspect connected tools is fulfilled by `search` or
`schema`. A request to read or change data in a named connected service must
continue through `call`; merely searching the service's tool catalog does not
count as performing that action. The runtime distinguishes these intents so
catalog questions do not loop into unnecessary service calls.

### The tool side

The broker's exposure is per provider round. When a conversation has no
connected server, the model is offered no MCP tool at all, and the round
reports `mcpExposureMode: none`.

The turn's own report names the exposure mode, the number of broker tools,
the calls used and remaining, the discovery calls used, and the result
characters used and remaining. Broker operations run by child agents draw from
the same per-turn call, discovery, and result budgets as their parent.

### The resource side

A connected MCP server may publish resources as well as tools. Three
native tools read them.

| Tool | Mode | What it does |
|---|---|---|
| `list_mcp_resources` | read | Lists the resources the connected servers publish. |
| `list_mcp_resource_templates` | read | Lists the parameterised resource URI templates. |
| `read_mcp_resource` | read | Reads one resource by its URI. |

The scope for the resource side is the same as the scope for the tool side,
plus the per-conversation switch. A server that the policy switches off for
one conversation keeps its resources out of reach for that conversation.

The listings are bounded. At most 100 pages are read from one server, one
listing returns at most 200 resources, and one template listing returns at
most 200 templates. One read returns at most 100 content items, and a
resource URI may hold at most 2 048 characters.

A server that fails does not hide what the other servers published. The
failure is reported only when no server answered at all.

A read asks the servers in turn, because only the publishing server knows
the URI. A server that does not publish the URI is not a failure of the
read. When no server publishes it, the error is "No connected MCP server
publishes that resource URI."

The resource path is proved against a real MCP server, over stdio and over
the `streamable_http` transport. The proof starts a server on the loopback
interface that publishes resources on two pages, a resource template, and a
tool, points the product's own integration at it, and reads the listing, the
pages, the text, the base64 binary, the template, and the answer to a URI
the server does not publish, through the bridge and through the three tools.
A second server that publishes tools and no resources answers the listings
with the "method not found" code, and the listing stays empty.

### A server that publishes no resources

A server that publishes tools but no resources answers the resource
listings with the JSON-RPC "method not found" code, `-32601`.

The bridge treats that answer as an empty listing, not a failure. The
server is skipped, it contributes nothing, and the listing of the other
servers stands. The bridge logs that the server publishes no resources and
carries on.

The same answer is accepted when a resource is read and no server publishes
the URI. The specification's own code for a URI that is not published is
`-32002`, the official SDK reports the same thing in the message text, and
the bridge accepts the code, the message, and a set of wordings such as
"not found", "unknown", "no such", and "not published".

The difference matters in practice. A deployment with one tool-only server
and one server with resources gets the resources, and the tool-only server
does not make the whole call look broken.

### The limits

Every limit is a `deepspace_mcp_*` setting.

| Setting | Default | What it limits |
|---|---|---|
| `deepspace_mcp_max_search_results` | 5 | Results one search page returns. |
| `deepspace_mcp_max_schema_chars` | 6 000 | The compact schema one schema call returns. |
| `deepspace_mcp_max_result_chars` | 12 000 | The characters one result may carry to the model. |
| `deepspace_mcp_max_calls_per_turn` | 8 | Remote tool calls one turn may make. |
| `deepspace_mcp_max_discovery_calls_per_turn` | 8 | Search, schema, and result calls one turn may make. |
| `deepspace_mcp_max_result_chars_per_turn` | 60 000 | The result characters one turn may carry. |
| `deepspace_mcp_max_argument_chars_per_call` | 20 000 | The characters one call's arguments may hold. |

When a budget is exhausted, the call is refused with a message that says
which budget ran out, and the turn continues with what it already has.

A result larger than `deepspace_mcp_max_result_chars` is not truncated
silently. The model receives an envelope with the status `truncated`, the
preview, the original size, the limit, and `more_available: true`.

### The routes

The MCP routes are under `/api/v1/mcp`.

| Route | What it does |
|---|---|
| `GET /api/v1/mcp/servers` | Lists the connected servers. |
| `GET /api/v1/mcp/servers/{server_id}` | Reads one connection. |
| `PATCH /api/v1/mcp/servers/{server_id}` | Changes one connection. |
| `POST /api/v1/mcp/servers/{server_id}/toggle` | Enables or disables one connection. |
| `GET /api/v1/mcp/servers/{server_id}/policy` | Reads the connection policy. |
| `GET /api/v1/mcp/servers/{server_id}/tools` | Reads the tool catalogue of one server. |
| `POST /api/v1/mcp/servers/{server_id}/refresh` | Refreshes the catalogue. |
| `POST /api/v1/mcp/servers/{server_id}/oauth/start` | Starts the authorization of a server. |
| `GET /api/v1/mcp/servers/{server_id}/oauth/callback` | Receives the authorization callback. |
| `DELETE /api/v1/mcp/servers/{server_id}/oauth` | Removes the stored credential. |
| `GET /api/v1/mcp/marketplace` | Lists the marketplace entries. |
| `POST /api/v1/mcp/marketplace/{entry_id}/connect` | Connects an approved provider. Local STDIO requests require `confirm_local_launch: true`. |
| `POST /api/v1/mcp/servers/custom` | Adds a custom MCP server; local STDIO requests also require explicit launch confirmation. |
| `GET /api/v1/mcp/catalog` | Lists the catalogue entries. |

The stored credential is encrypted, and the plaintext never enters the
table that holds it.

### Connectors

A connector is a tenant-owned instance of an integration. It fetches data
from an outside system on a schedule and feeds it into the ingestion
pipeline, where it becomes documents the product can search. The schedule
is the connector's `sync_frequency`, `daily` by default, and the connector
records the time of its last sync and the time of its next one.

A connector is not a tool and the agent does not call it. The routes are
under `/api/v1/integrations`.

| Route | What it does |
|---|---|
| `GET /api/v1/integrations/connectors` | Lists the tenant's connectors. |
| `GET /api/v1/integrations/connectors/summary` | Returns a fleet summary for operations. |
| `POST /api/v1/integrations/connectors` | Creates one connector. |
| `POST /api/v1/integrations/connectors/{connector_id}/sync` | Runs one sync now. |
| `GET /api/v1/integrations/connectors/{connector_id}/summary` | Returns what the connector holds. |
| `PATCH /api/v1/integrations/connectors/{connector_id}` | Changes one connector. |
| `DELETE /api/v1/integrations/connectors/{connector_id}` | Removes one connector. |

A connector's own credential is encrypted at rest, in the same way an MCP
credential is.

## What to watch for

- The tool catalogue the model sees is a summary. A tool whose name and
  description do not match the model's words is not found by a search.
- A search returns at most 5 results at a time, so a broad question needs
  more than one search.
- The search is a keyword match over the server name, the tool name, the
  title, the description, and the parameter names. It does not understand
  the meaning of a phrase.
- The exact schema stays on the server. When the compact schema is not
  enough to build a valid call, the call is refused.
- A call that names a `tool_ref` the search did not return is refused, and
  the model cannot supply a raw upstream name instead.
- A tool whose risk is above the connection's ceiling is refused before it
  runs, and a read-only connection refuses every write.
- A server with a stale catalogue keeps its last known tools while the
  refresh runs, so a tool the server has since removed may still be
  offered.
- A catalogue revision that moved between the search and the call is
  refused, with the reason that the tool list must be refreshed.
- The result budgets are per turn. A conversation that asks for a lot from a
  connected service runs out and is told so.
- A stored result is kept for 900 seconds under the tenant, the user, and
  the conversation, and it is gone after that.
- A server that publishes no resources is treated as an empty listing. A
  listing that comes back empty therefore does not prove that no server
  publishes resources.
- A resource read asks the servers in turn, so the read is slower with many
  connected servers.
- A redirect is not followed. The client refuses a redirect answer, so an
  MCP server that redirects its endpoint, a server that answers `/mcp` with
  a redirect to `/mcp/` for example, has to be connected with the URL it
  answers on.
- Connectors and MCP servers are separate. A document a connector brought in
  is searchable in the product, and it is not reachable through `mcp_broker`.

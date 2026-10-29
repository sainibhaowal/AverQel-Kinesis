# The tenant scope on a database session

Every query in the platform runs under a tenant scope. The scope is the
row-level security context the query is evaluated against, and it is the
boundary that keeps one workspace's rows out of another's.

## How the scope is applied

`set_db_tenant_context(session, tenant_id)` binds the scope to the
session. The request path applies it through the `get_tenant_context`
dependency, so every request-scoped query runs under the tenant the
request belongs to.

The scope is **session-scoped**: it survives a commit inside the request.
A commit used to end the scope, and a query that followed a write then ran
without one. The database returned `invalid input syntax for type uuid`,
and a query that did not fail could have matched another tenant's rows.
Every write path in the platform commits, so this was the normal case and
not an edge case.

The scope is cleared when the session's connection goes back to the pool,
so a connection never carries one request's scope into the next request.
The clearing happens in two places, and both are needed:

- the request path clears the scope in its session cleanup;
- the pool clears the scope on every connection it hands back, which
  covers the worker paths and any session the platform did not open
  itself.

The reset value is an identity no tenant owns, so a connection that is
reused without a scope matches no row.

## The cross-tenant staff scope

A few surfaces read across tenants on purpose: the support desk reads
tickets from every workspace, and the feedback review reads submissions
from every workspace. Those surfaces use the deployment's own `bypass`
scope, and they require a permission that only an explicit staff
assignment grants:

| Surface | Permission | Scope |
|---|---|---|
| Support queue, ticket, and attachment reads | `admin:support:read` | `bypass` |
| Support ticket assignment and replies | `admin:support:write` | `bypass` |
| Feedback submission list and detail | `admin:feedback:read` | `bypass` |
| Feedback review replies | `admin:feedback:write` | `bypass` |

A staff role is never implied by a normal or platform-admin account. The
support desk additionally requires the deployment's platform-admin
allowlist, because its tickets span tenants.

The staff responses carry no account identifier and no contact detail, so
a reviewer sees the report and not the person who sent it. Every
cross-tenant read is written to the audit log before the response is
returned.

## What to check when a query returns the wrong rows

1. Does the path set the scope? A query path that reads a tenant-scoped
   table without `get_tenant_context` runs under whatever scope the
   connection carried.
2. Does the path commit before it reads? The scope survives a commit, so
   a read after a write is still scoped.
3. Is the path a staff surface? A staff surface uses the `bypass` scope
   deliberately, and it carries a permission check.
4. Did the path clear the scope? A session that is closed without
   clearing leaves the scope on its connection.

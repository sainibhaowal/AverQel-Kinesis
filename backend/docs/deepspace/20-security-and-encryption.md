# 20. Security and encryption

This chapter describes the controls that keep one account's work away from
another's, and the controls that keep the deployment's secrets away from the
agent. It covers the bearer token and its refresh, the local mode that
needs no login, what is stored encrypted and what is not, the reduced
environment a command receives, the jail extension code runs in, and the
path checks that keep a tool inside its workspace.

## Use case

- An operator has to decide what a deployment may run, and where the
  secrets live.
- A developer has to know why a request from another tenant's id fails the
  same way a request for a conversation that does not exist fails.
- A security reviewer has to know which data is encrypted and which is not.
- A user running the app locally has to know that there is no login and what
  that means.
- A person writing an extension has to know what the extension can and
  cannot reach.

## Why it matters

- One tenant's file must never be readable through another tenant's
  conversation id. A difference in the answer would tell an attacker that a
  resource exists.
- The agent runs code. The environment that code receives decides whether a
  provider key or a database password is one `printenv` away.
- An extension is code the deployment did not write. It must not be able to
  read the server's memory.
- A tool that can write outside its workspace can change the host. The path
  checks are the last line before the file system.
- Saying "the data is encrypted" when only part of it is leads a deployment
  to leave the rest unprotected.

## How it works

### Tenant isolation

Every row DeepSpace owns carries a tenant id and a user id. A conversation,
a session, a workspace, a file, and a tool call all belong to one tenant and
one user.

The request path applies the scope with the `get_tenant_context`
dependency, which calls `set_db_tenant_context` on the database session. The
scope is bound to the session, so it survives a commit inside the request.
It is cleared when the connection goes back to the pool, and the pool
clears it on every connection it hands back, so one request's scope never
reaches the next. The reset value is an identity no tenant owns, so a
connection reused without a scope matches no row. The full description is in
`backend/docs/platform/01-tenant-scope.md`.

A request may also carry an `X-Tenant-Id` header. When it does, it must
match the tenant in the access token. A mismatch is refused with
`TENANT_SCOPE_MISMATCH` and status 403.

A few surfaces read across tenants on purpose, the support desk and the
feedback review. They use the deployment's `bypass` scope and need an
explicit staff permission, and every such read is written to the audit log
before the response is returned.

### Authentication: the bearer token and its refresh

The API is authenticated with a bearer access token.

| Setting | Default | What it decides |
|---|---|---|
| `jwt_algorithm` | `HS256` | The signing algorithm of the access token. |
| `jwt_issuer` | `ai-knowledge-service` | The `iss` claim. |
| `jwt_audience` | `ai-knowledge-service-api` | The `aud` claim. |
| `jwt_access_ttl_minutes` | `15` | How long an access token is valid. |
| `jwt_refresh_ttl_days` | `7` | How long a refresh token is valid. |
| `refresh_cookie_name` | `aks_refresh_token` | The name of the refresh cookie. |
| `refresh_cookie_path` | `/api/v1/auth` | The path the refresh cookie is sent to. |
| `refresh_cookie_secure` | `true` | Whether the refresh cookie is marked Secure. |
| `refresh_cookie_samesite` | `strict` | The SameSite policy of the refresh cookie. |
| `auth_max_failed_attempts` | `5` | Failed sign-in attempts before the account is locked. |
| `auth_lockout_minutes` | `15` | How long the lock lasts. |
| `auth_session_retention_days` | `90` | How long a sign-in session record is kept. |

The access token carries these claims: `sub` (the user), `tenant_id`,
`roles`, `jti` (the token's own id), `typ`, `ver` (the user's token
version), `iat`, `nbf`, `exp`, `iss`, `aud`, and `sid` when the token
belongs to a recorded sign-in session.

A token is refused when:

- The signature, issuer, audience, or expiry does not check out. The codes
  are `INVALID_ACCESS_TOKEN`, `ACCESS_TOKEN_EXPIRED`, and
  `ACCESS_TOKEN_NOT_YET_VALID`.
- The token's `jti` is on the revocation list. The list is checked in Redis
  first, under `jwt:deny:{token_id}`, and in the database second.
- The user's `access_token_version` has moved on. Changing the version
  invalidates every token the user holds at once.
- The sign-in session the token names has been revoked. The code is
  `SESSION_REVOKED`.
- The user is inactive (`USER_DISABLED`) or the tenant is inactive
  (`TENANT_DISABLED`).

A token whose name starts with `dx_` is an API key rather than a JWT. Its
SHA-256 hash is looked up, its scopes become the request's permissions,
and an expired or revoked key is refused.

The refresh token is a separate, longer-lived secret. The raw value is put
in an HTTP-only cookie and never reaches the browser's script. The server
stores only an HMAC-SHA256 digest of it, computed with
`refresh_token_hash_secret`, so a database copy does not let anyone sign in.
The browser calls `POST /api/v1/auth/refresh` with the cookie and receives
a new access token and a new refresh token.

The browser keeps the access token in local storage under `averqel_token`,
the tenant under `averqel_tenant_id`, and the user record under
`averqel_user`. It reads the token's own `exp` claim and refreshes before
the token expires. A lock in local storage keeps two tabs from refreshing
at the same time, and a tab that loses the race waits for the other tab to
publish the new token.

### Password hashing

Passwords are hashed with Argon2id. The parameters live in
`app/auth/security.py`:

| Parameter | Value |
|---|---|
| Time cost | 3 |
| Memory cost | 65536 KiB |
| Parallelism | 4 |
| Hash length | 32 bytes |
| Salt length | 16 bytes |
| Minimum password length | 8 characters |

`hash_password` refuses an empty password. `verify_password` returns false
for an empty password, an empty hash, a mismatch, and a hash it cannot
read. `password_hash_needs_rehash` reports whether a stored hash should be
upgraded to the current parameters.

### Local mode and the local operator

When the app runs on its local SQLite store, a request with no usable token
is signed in as the local operator. The check is on the database dialect,
so it applies to SQLite and to nothing else.

| Property | Value |
|---|---|
| Tenant | `00000000-0000-0000-0000-000000000001` |
| User | `00000000-0000-0000-0000-000000000002` |
| Roles | `admin`, `staff`, `feedback_manager`, `collection_moderator`, `support_manager` |
| Permissions | Every permission, written as `*` |
| Token id | `local-token` |
| Auth type | `local` |

A request is treated as the local operator when the token is missing,
empty, or one of the literals `local-token`, `null`, and `undefined`. The
browser writes `local-token` into local storage on first load, so the
browser and the server agree.

`run_backend.py` seeds the local tenant, the local roles, and the local
user on a SQLite start. The seeded user is `admin@averqel.local` with the
password `local_first_nopass`, stored as an Argon2 hash.

The local mode is the local, free, single-machine experience. The Docker
deployment uses PostgreSQL and real logins.

### What is stored encrypted

| Data | How it is protected |
|---|---|
| Provider API keys | AES-GCM, 12-byte random nonce, keyed by a key id. The key comes from the deployment's keyring or from AWS KMS. |
| Connector and MCP OAuth secrets | The same AES-GCM scheme, with a separate keyring when the deployment names one. |
| Provider continuation blobs | The same AES-GCM scheme. The additional data names the tenant, the user, the conversation, the message, and the turn index, so a blob cannot be moved to another place. |
| The typed session log payload | AES-GCM, a 12-byte random nonce per row, keyed by the deployment session-log keyring or the local key file in single-process local mode. |
| Passwords | Argon2id. |
| Refresh tokens | HMAC-SHA256 digest with the deployment's secret. |

The crypto lives in `app/providers/services/provider_secret_crypto.py` and
`app/integrations/services/connector_secret_crypto.py`. The keyring is
named by `provider_secret_backend`, which is `env_keyring` or `aws_kms`.
With the `env_keyring` backend, `provider_secret_keyring_json` holds a JSON
object of key ids to base64 keys, and each key must decode to 16, 24, or 32
bytes for AES-GCM. `provider_secret_active_kid` names the key in use.
`provider_secret_audit_reads` decides whether reading a secret is written to
the audit log.

The local home directory has a `secrets/` folder for deployment-level
secret files, such as the Composio key. The provider and connector secrets
themselves are rows in the database, encrypted. The folder sits beside
`db/`, `storage/`, `cache/`, `mcp/`, `logs/`, `exports/`, and `models/`
under `~/.averqel`, or under the directory named by the `AVERQEL_HOME`
environment variable.

### The session log at rest

The `payload` column of `deepspace_session_events` is encrypted with AES-GCM before it reaches the database. Each payload becomes an envelope that carries `__encrypted__` set to `1`, the key id, a 12-byte random nonce, and the ciphertext, with the nonce and the ciphertext encoded as base64url. The column type stays `JSON`, so no schema change or migration is involved, and an ORM read returns the plaintext payload as before.

A deployment sets `deepspace_session_log_keyring_json` and `deepspace_session_log_active_kid` together. The keyring is a JSON object of key ids to base64url keys, and each key must decode to 16, 24, or 32 bytes for AES-GCM. Production rejects the default local-file fallback and requires this shared keyring so API workers, Celery workers, and other processes can read the same rows. When neither setting is present in local development, the first write creates `<averqel home>/keys/deepspace-session-log.key` atomically with mode 0600 and 32 random bytes. The `AVERQEL_HOME` environment variable moves that file, so the desktop app keeps it inside its user-data directory. A deployment whose AverQel home is read-only, such as a container with a read-only root filesystem and no home volume, must set the keyring.

To rotate the key, add a new key id to the keyring, point `deepspace_session_log_active_kid` at it, and keep the old key until every row written under it has been re-encrypted or is no longer needed. A row carries the key id that encrypted it, so old rows stay readable while the old key remains in the keyring. There is no automatic re-encryption pass. The local key file holds one key, so replacing it makes earlier rows unreadable; use the keyring when old rows must survive a rotation. The local key file is read once per process, so restart the process after replacing it.

Only the payload is encrypted. The row's ids (`id`, `tenant_id`, `user_id`, `conversation_id`, `run_id`, `subagent_id`), `seq`, `event_type`, `turn`, `step`, `tool_name`, and `created_at` stay readable so the sequence, the tenant scope, the event-type lookups, and the projections keep working. The frame log and the conversation messages are separate records and are not covered by this key. A payload written before encryption was enabled stays plaintext until the row is rewritten.

Encryption is on by default through `deepspace_session_log_encryption`. When it is on and the key cannot be loaded, the cipher raises `SessionLogCryptoError` and the write fails; no plaintext payload is stored. When a stored envelope cannot be authenticated, a read raises it rather than returning the ciphertext. Turning encryption off writes new payloads as plaintext but still decrypts envelopes written earlier.

### The tool environment

Three different environments exist, and they are not the same.

**A terminal session** receives a fixed list of variables and nothing else:

| Variable | Value |
|---|---|
| `PATH` | The server's search path. |
| `HOME` | The workspace root. |
| `TMPDIR` | A temporary directory inside the workspace. |
| `LANG` | The server's locale. |
| `TERM` | `xterm-256color`. |
| `DEEPSPACE_SHELL` | `1` |
| `DEEPSPACE_SANDBOX` | `1` |
| `DEEPSPACE_SANDBOX_MODE` | The containment mode the session runs in. |
| `DEEPSPACE_SHELL_KIND` | The shell the session runs. |
| `DEEPSPACE_WORKSPACE` | The workspace root. |

The function that builds it is `build_session_environment` in
`app/deepspace/services/terminals.py`. The server's own environment is not
inherited, because it holds the deployment's credentials.

**A plugin command** receives `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`,
`SYSTEMROOT`, `TEMP`, and `TMP`, with `HOME` set to the workspace when the
caller knows one, plus `DEEPSPACE_PLUGIN=1`. The function is
`plugin_environment` in `app/deepspace/services/plugins.py`.

**The `bash` and `pwsh` tools** receive the same reduced environment as a
terminal session: the search path, the locale, the workspace as the home
directory, a temporary directory inside the workspace, and the DeepSpace
markers. The server's own environment is not inherited, so a deployment
secret that lives in an environment variable is not within reach of a shell
command. The function that builds it is `build_session_environment` in
`app/deepspace/services/terminals.py`, the same one the terminal sessions
use.

### The extension jail

Extension code runs in its own process, inside the operating-system cage.
Two things follow:

- The extension cannot read the server's memory, because it is a different
  process.
- The extension is confined to the workspace, because the cage is the same
  one the shell commands use.

A host that offers no cage refuses to run extension code. The refusal
names the runners the platform offers so the operator knows what to
install. `deepspace_plugin_code_enabled` turns extension code off
entirely, and `deepspace_sandbox_required` makes a missing cage refuse
every command rather than run it uncontained.

A workspace plugin's Python handler is imported inside the cage from the
workspace file itself, and the workspace root is on the import path while
the module loads, so the module's own imports resolve inside the workspace
first.

### Path confinement

Every path a tool resolves is checked against the workspace root.

The path is normalized first. An empty path, an absolute path, a path with a
`..` segment, a path longer than 1024 characters, and a path with more than
64 segments are all refused. The normalized path is joined to the root and
resolved, and then checked again against the root. The second check is what
stops a symbolic link inside the workspace from pointing at a file outside
it.

The directories are laid out like this:

| Directory | Path |
|---|---|
| Base | `deepspace_workspace_root`, or `<local_storage_path>/workspaces` when that is empty. |
| One account's default workspace | `<base>/<tenant id>/<user id>` |
| One account's named workspaces | `<base>/<tenant id>/<user id>.workspaces/` |
| One named workspace | `<base>/<tenant id>/<user id>.workspaces/<workspace id>` |

A workspace identifier may hold letters, digits, dashes, underscores, and
dots, and may be at most 64 characters. A named workspace sits beside the
default one rather than inside it, so a conversation working in one cannot
reach the others through the file tools. A symbolic link out of the
account's own directory is refused when the workspace is created.

A deployment with no configured base has no workspace at all, and the file
and shell tools report that rather than writing somewhere else.

### An id that never reveals that something exists

A conversation is looked up by tenant, user, and conversation id together.
A conversation belonging to another tenant is not found, in the same way a
conversation that does not exist is not found. Both answers are the same
404 with the code `CONVERSATION_NOT_FOUND` and the message "Conversation not
found".

The same rule applies to the workspaces, the library files, the sessions,
the schedules, and the tool calls. The rule holds because the tenant and
the user are part of the lookup, not a check that happens afterwards.

## What to watch for

- Local mode has no login. Anyone who can reach the local API is the local
  operator with every permission. The rule applies only while the database
  dialect is SQLite, so a Docker deployment with PostgreSQL is unaffected.
- The seeded local user has a fixed password, `local_first_nopass`. It is
  hashed on disk, and it is still a known value.
- The typed session log's payload is encrypted with the deployment key, but the rest of the row, the frame log, and the conversation messages are not. Protect the database volume as well if those must be unreadable at rest.
- The session search filters the payload column in SQL before it loads a row. An encrypted payload does not match that filter, so a term that appears only in a payload is not found by `session_search` or `session_event_search`; a term in the event type, the tool name, or a legacy plaintext payload still matches.
- The `bash` and `pwsh` tools receive a reduced environment, so a
  deployment secret in an environment variable is not within reach of a
  command. Keep deployment secrets in the database's encrypted secret store
  as well, because a deployment that starts a command without a cage (or
  with the full-access permission mode) still runs it with the server's
  rights.
- The default `jwt_secret` and `refresh_token_hash_secret` are placeholders
  that begin with `change-me`. A production start refuses to run with them,
  and `scripts/validate_prod_env.sh` rejects any value containing
  `change-me` or shorter than 32 characters.
- `refresh_cookie_secure` is `true` by default, and production requires it.
  A browser does not keep a Secure cookie over plain HTTP, so a local
  deployment that serves HTTP needs `AKS_REFRESH_COOKIE_SECURE=false` if it
  uses the refresh flow at all.
- The `bash` tool is contained by the cage, not by the environment. The
  containment depends on the host offering a runner. The Linux proof in
  `backend/tests/unit/test_deepspace_linux_cage_proof.py` skips on a host
  that cannot create the namespaces, so the check is not evidence on every
  machine.
- The `X-Tenant-Id` header is a convenience for the login flows. It does
  not grant access: a mismatch against the token's tenant is refused.
- The `bypass` scope exists for the support and feedback surfaces. Those
  permissions are never implied by an ordinary account.
- The Argon2 parameters cost about 64 MiB and a few hundred milliseconds
  per hash. A sign-in burst costs more CPU than the rest of the request
  path.

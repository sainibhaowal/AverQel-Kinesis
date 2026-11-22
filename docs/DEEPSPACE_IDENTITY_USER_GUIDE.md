# DeepSpace Anonymous Identity User Guide

**Date:** 2026-11-19

**Audience:** Users and integrators who need to know what the anonymous installation identifier is and where it comes from.

---

## Overview

DeepSpace can mint a **stable, anonymous installation identifier**: a random UUID stored in a file under the AverQel home directory. It survives restarts and updates, identifies an installation rather than a person, and can be read or reset through the host API.

It contains no name, email, account id, hostname, MAC address, or IP address. Two different home directories produce two different identifiers.

---

## Where It Lives

| Item | Value |
|---|---|
| File | `<home>/.anonymous-user-id` |
| Format | One UUID and a newline, e.g. `9119474c-1234-5678-90ab-cdef12345678` |
| Scope | One AverQel home directory |
| Default home | `~/.averqel-harness` |

Home resolution order (first set value wins):

1. `AVERQEL_HOME`
2. `AKS_HOME`
3. `DSH_HOME`
4. `~/.averqel-harness`

The serving Electron shell sets `AVERQEL_HOME` for the packaged app.

---

## How It Works

1. The service checks its in-process cache for the home directory.
2. If the file exists and contains a valid UUID, that value is returned.
3. Otherwise it mints a random UUID (Python's `uuid.uuid4()`), writes it followed by a newline, and caches it.
4. On the next request in the same process the cached value is returned without another write.
5. `reset_id()` deletes the file, clears the cache, and mints a new UUID immediately.

### File and process behaviour

| Situation | Behaviour |
|---|---|
| First run, no file | A UUID is minted and written |
| File present and valid | The stored UUID is reused |
| File corrupt or empty | A new UUID replaces it |
| File is unwritable | The process still receives a UUID for this session; it is not persisted |
| Home directory missing | The directory is created first |
| Two processes start together | Both may write; the last write wins on disk. There is no cross-process lock |
| File deleted while running | The next process mints a fresh ID; a running process keeps its cached value |
| Different home directory | A different identifier |

Deleting the file is the supported way to opt out of continuity and get a fresh identity.

---

## HTTP API

The host routes require authentication.

```http
GET /api/v1/host/identity/anonymous-id
```

```json
{"user_id": "9119474c-1234-5678-90ab-cdef12345678"}
```

```http
POST /api/v1/host/identity/anonymous-id/reset
```

```json
{"user_id": "6d1f0c2a-...-new-uuid"}
```

Reset deletes the file and returns the newly minted identifier. Both routes return the same `user_id` field.

---

## Where the Identifier Is Used

| Consumer | Status |
|---|---|
| Host identity API | Implemented: `GET` and `reset` above |
| Integrations that call the identity service | Available through the service methods below |
| Session telemetry | The telemetry service does not currently read this identifier |
| DeepSeek provider requests | No Kinesis code sends the `x-deepseek-harness-user-id` header |
| Feedback acknowledgement | Feedback records do not currently include the identifier |

Treat the identifier as an available installation marker, not as a guarantee that every subsystem attaches it. The host API is the supported way to read it today.

---

## Programmatic Access

```python
from app.deepspace.services.host import create_anonymous_user_id_service

service = create_anonymous_user_id_service()
user_id = service.get_or_create_id()
```

| Method | Purpose |
|---|---|
| `get_home_dir()` | Resolve and create the home directory |
| `get_id_file_path()` | Return `<home>/.anonymous-user-id` |
| `get_or_create_id()` | Read or mint the identifier |
| `reset_id()` | Delete the file and mint a new identifier |
| `get_id_for_telemetry()` | Alias of `get_or_create_id()` |
| `get_id_for_feedback()` | Alias of `get_or_create_id()` |
| `get_id_for_deepseek()` | Alias of `get_or_create_id()` |

The three `get_id_for_*` methods exist for callers that attach the identifier to their own records; they do not change what the runtime sends on their behalf.

The host API wraps one process-wide service instance, so repeated requests in one API process reuse the cached value even if the file is deleted externally.

---

## Privacy Properties

| Property | Implementation |
|---|---|
| Random | UUID v4 from the operating system's random source |
| Not derived | No hostname, MAC address, git remote, or account data is mixed in |
| Home-scoped | Scoped to the AverQel home directory, not the machine or the user account |
| No registry | The value is written only to the local file; nothing registers it centrally |
| User-controlled | Delete the file or call reset at any time |
| Best-effort persistence | A write failure still yields a session identifier |

The identifier is not a device fingerprint, but it is stable per home directory. Anyone who can read that file can read the identifier.

---

## Viewing and Resetting

View the file directly:

```bash
cat "${AVERQEL_HOME:-$HOME/.averqel-harness}/.anonymous-user-id"
```

Reset it through the API:

```bash
curl -X POST http://localhost:10000/api/v1/host/identity/anonymous-id/reset \
  -H "Authorization: Bearer <token>"
```

Or delete the file and restart the backend:

```bash
rm "${AVERQEL_HOME:-$HOME/.averqel-harness}/.anonymous-user-id"
```

---

## Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| A new ID on every launch | The home directory is not persisted or is different each launch | Pin `AVERQEL_HOME` to a stable directory |
| Different IDs in different tools | Tools resolve different home directories | Set one `AVERQEL_HOME` for the whole deployment |
| ID present on disk but the API returns another | The long-running process cached the earlier value | Restart the backend or call the reset route |
| File is not created | The home directory is unwritable | Fix permissions; the in-memory ID still works for the session |
| Two backends report different IDs | Both processes raced on first write | Let one process initialize the home before starting the other |

---

## Related Guides

- [DeepSpace Settings Forms Guide](DEEPSPACE_SETTINGS_FORMS_USER_GUIDE.md) — Deployment settings
- [DeepSpace Permission Guide](DEEPSPACE_PERMISSION_USER_GUIDE.md) — Authentication boundaries
- [DeepSpace Open In App Guide](DEEPSPACE_OPEN_IN_APP_USER_GUIDE.md) — Host integrations

---

*Last updated: 2026-11-19*

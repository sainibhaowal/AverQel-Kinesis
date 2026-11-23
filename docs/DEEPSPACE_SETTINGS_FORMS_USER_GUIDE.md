# DeepSpace Settings Forms — User Guide

**Date:** 2026-11-19

---

## What It Is

**Settings Forms** is an API that projects the deployment's live Pydantic settings schema into a form description: one namespace, many sections, and one entry per field with its type, default, range, enum values, and UI hints.

It is useful for:

- Inspecting every setting the running backend knows about
- Building an admin form without duplicating the schema
- Checking a field's type, bounds, and enum before sending a patch
- Seeing which fields the API treats as secrets

**What it is not (in the Kinesis build):** the desktop **Settings → DeepSpace** page does not render this form. That page manages sandbox runtime and agent presets through other endpoints. The write side of Settings Forms is revision-checked but does not rewrite the running process's configuration; treat it as a schema and revision API, not as a live configuration editor.

---

## What You See in the API

`GET /api/v1/deepspace/chats/settings/forms` returns:

| Field | Meaning |
|---|---|
| `namespace` | Always `deepspace` |
| `revision` | Current write revision for the namespace (0 before the first write) |
| `sections` | Fields grouped by the prefix of each setting's name |
| `value` | Effective values, with secrets replaced by `"********"` |
| `base` | The schema defaults |
| `user` | User overrides (empty in this build) |
| `secrets` | Keys that were redacted |

The form currently has **53 sections** and **265 fields in the `deepspace` section** alone. Sections are named after the first token of each setting, which is why you see `deepspace`, `auth`, `database`, `storage`, `celery`, and so on rather than the product's subsystem names. The `deepspace` section holds the DeepSpace runtime settings; related fields such as `deepspace_shell_enabled` and `deepspace_terminal_enabled` live there too.

Example section keys: `AKS`, `AVERQEL`, `admin`, `api`, `auth`, `celery`, `database`, `deepspace`, `storage`, and others.

---

## Field Types

| Type | UI widget you would render | Validation |
|---|---|---|
| `string` | Text input | min/max length, pattern |
| `integer` | Number input | minimum/maximum |
| `number` | Number input | minimum/maximum |
| `boolean` | Checkbox | — |
| `enum` | Dropdown | must match an allowed value |
| `array` | Multi-select / tag input | item validation |
| `object` | Nested form | nested validation |

Each field also carries `default`, `required`, `description`, and `ui_hints` (including `group`). Secret fields are marked `"secret": true`.

### Secret detection

The API marks a field secret when its key contains `password`, `secret`, `token`, `key`, or `api_key`, or when the schema marks it secret. The name match is literal, so a counter such as `deepspace_compaction_llm_max_tokens` is also redacted even though it is not a credential. Secret values are returned as `"********"`.

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/deepspace/chats/settings/forms` | Describe the namespace and every section |
| `GET` | `/api/v1/deepspace/chats/settings/forms/{namespace}/ui-schema` | JSON Schema plus UI schema |
| `PUT` | `/api/v1/deepspace/chats/settings/forms/{namespace}` | Revision-checked merge patch |
| `POST` | `/api/v1/deepspace/chats/settings/forms/{namespace}/replace` | Revision-checked replace |
| `POST` | `/api/v1/deepspace/chats/settings/forms/{namespace}/mutate` | Path operations |
| `POST` | `/api/v1/deepspace/chats/settings/forms/import` | Import a legacy settings file |

The only accepted `{namespace}` value is `deepspace`.

### Write responses

`PUT`, `replace`, and `mutate` return a new revision and `status: "updated"`. The revision is checked against the request's expected revision; a mismatch returns a conflict. The running process's configuration is not rewritten by these calls — use environment variables or the deployment's settings file to change behavior.

### Revision conflict

```
Settings namespace 'deepspace' changed since read
(expected revision 5, actual 7)
```

Reload the form, re-apply the change, and save again with the current revision.

---

## Path-Based Mutations

The mutate endpoint accepts path operations:

```json
{
  "ops": [
    {"op": "set", "path": "/deepspace_shell_timeout_seconds", "value": 180},
    {"op": "unset", "path": "/deepspace_terminal_idle_timeout_seconds"},
    {"op": "add", "path": "/custom_array/-", "value": "new_item"}
  ]
}
```

Supported operations: `set`, `unset`, `add` (append with `-`), `remove`, `move`, `copy`, and `test`.

---

## UI Schema

`GET /api/v1/deepspace/chats/settings/forms/deepspace/ui-schema` returns:

```json
{
  "json_schema": {"type": "object", "properties": {}, "required": []},
  "ui_schema": {"field_key": {"ui:widget": "password", "ui:placeholder": "..."}}
}
```

The shape is compatible with React JSON Schema Form style renderers. The Kinesis desktop UI does not use it today.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `Namespace X not found` | Any value other than `deepspace` | Use `deepspace` |
| Revision conflict | Concurrent write or a missing expected revision | Reload and retry with the current revision |
| A field is missing from `base` | The field has no concrete default | Read it from `value` instead |
| A non-secret field is redacted | Its name contains `token` or `key` | This is the name-based rule; do not rely on the redaction list as a credential list |
| A saved change does not alter behavior | The write API does not apply configuration | Change the deployment environment or settings file and restart |

---

## Technical Details (If You're Curious)

- **Schema projection** — `Settings.model_json_schema()` is converted into typed form fields
- **Sectioning** — fields are grouped by the leading token of the setting name
- **Revision store** — database-backed counter per namespace
- **Redaction** — recursive walk with name-based and schema-based secret detection
- **UI hints** — `ui_hints.group` and widget hints are passed through unchanged

---

## Related Documentation

| Topic | Document |
|---|---|
| Settings API | `backend/app/deepspace/api/workspace.py` |
| Forms service | `backend/app/deepspace/services/settings_forms.py` |
| Pydantic settings | `backend/app/core/config.py` |
| Runtime settings page | `frontend/app/dashboard/settings/deepspace/page.tsx` |

---

**TL;DR:** Use `GET /api/v1/deepspace/chats/settings/forms` to inspect the live schema and field metadata. The form is one `deepspace` namespace split into 53 prefix-based sections. The desktop settings page is separate, and the write endpoints do not change the running configuration.

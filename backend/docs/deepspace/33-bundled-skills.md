# 33. Skill discovery, invocation, and Office workflows

DeepSpace resolves skill instructions from trusted workspace and shared roots,
then merges them with enabled bundled definitions and runtime-published skills.
Skills are plain Markdown; the registry supplies discovery, ranking, invocation
policy, and resource loading. The `office_process` native tool adds bounded
LibreOffice and PDF rendering operations for a workspace.

## Use case

- A project wants its own procedures to take precedence over a user's shared
  guidance.
- A user wants to invoke a known skill directly with `/skill-name request` or
  `@skill:name request`.
- A skill needs a template or other resource next to its instruction file.
- An agent needs a workspace-safe PDF export, spreadsheet recalculation, or
  page render.
- A child agent must see only the skills its parent granted it.

## Why it matters

- Ranked discovery avoids arbitrary duplicate resolution and keeps skill
  content within configured roots.
- Frontmatter can separately permit model use and direct user invocation.
- Relative resource reads and user slash commands use the same resolved
  definition as `skill_list` and `skill_load`.
- Office operations use a private copy, enforce workspace output paths, and
  return a clear unavailable result when the required runtime is missing.
- A child agent inherits a scoped catalog so its parent's skill grants are
  enforced even when the child resolves the same workspace roots.

## How it works

### Discovery and provider precedence

`SkillRegistry` merges filesystem, bundled, remote, and runtime providers. The
providers are scanned in rank order; the first definition with a given name
wins:

| Rank | Root | Scope |
|---:|---|---|
| 100 | `<workspace>/.kinesis/skills` | Project KineSis skills |
| 200 | `<workspace>/.agents/skills` | Project agent skills |
| 250 | Runtime-published skills | Skills published by a running process (e.g., editor extensions) |
| 300 | `deepspace_skill_custom_roots` | Explicit absolute roots, split with the platform path separator |
| 350 | Signed remote catalog | Optional HTTPS catalog verified with its configured Ed25519 public key |
| 400 | `<AVERQEL_AGENTS_HOME>/.kinesis/skills` | Shared KineSis skills in the desktop runtime |
| 500 | `<AVERQEL_AGENTS_HOME>/skills` | Shared agent skills in the desktop runtime |
| 600 | Enabled bundled definitions | Product skills |

A root may contain flat `name.md` files or directories with `SKILL.md`,
`skill.md`, or `README.md`. Discovery rejects symlinks that leave a configured
root and ignores malformed or oversized definitions. It rescans roots when a
list or lookup is requested. Subscribing to a registry establishes a baseline
and starts a Python polling watcher; it rescans once per second and sends
`skills/change` to listeners when the resolved catalog changes. The daemon
watcher stops when the last listener unsubscribes. This avoids an external
filesystem-watcher dependency; notifications have up to one second of polling
latency. A remote catalog is used only when both its HTTPS URL and Ed25519
public key are configured. The signature covers the canonical skill list;
invalid signatures and malformed definitions are rejected.

### Runtime skill provider

A running process can publish ephemeral skills at rank 250 via
`registry.register_runtime_skill(definition)`. This is intended for editor
extensions and ephemeral tool integrations that want to expose a procedure
without writing files. The runtime provider is created on first use; the
returned callback removes the skill again. These skills are not persisted and
are visible only within the process that registered them.

### Frontmatter and invocation

The registry reads `name`, `description`, `when-to-use`, `model-invocable`, and
`user-invocable` scalar frontmatter. The default name is the file or directory
name. The instruction body is capped at 40 000 characters, and the source file
at 160 000 bytes.

`skill_list` shows definitions invocable by the model. `skill_load` loads a
model-invocable definition and marks `truncated: true` when the body exceeded
the 40 000 character limit. `skill_resource` reads a relative file under that
skill's resource base; it refuses traversal and external symlinks and returns
binary data as base64.

A user can directly invoke a skill with two equivalent forms:

- `/skill-name request` — a whole-prompt slash command.
- `@skill:skill-name request` — an at-mention style command.

Both expand only when the command occupies the entire prompt (no preceding
text). The backend injects the skill's instructions wrapped in
`<skill_content>` and appends the user's request text. When
`deepspace_skill_user_invocation_enabled` is true and the skill allows user
invocation, the expansion succeeds. A skill marked `user-invocable: false` is
refused with HTTP 403. A model-only skill remains available to the model but is
excluded from user-invocation discovery.

### Skill validation

`skill_validate` checks a draft skill document before it is installed. It
reports:

- Frontmatter presence and structure.
- Valid skill name (kebab-case by default; underscores rejected only when
  `deepspace_skill_strict_names` is true).
- A missing or empty frontmatter description warning, plus an instruction
  length warning.
- Duplicate name warnings against the current workspace catalog.
- Resource reachability when a `resource_base` is supplied (prevents path
  traversal).

The tool returns `{ valid: true, issues: [] }` on success; each issue has
`code`, `message`, and `severity` (`error` or `warning`). A deployment can
require validation before accepting a skill from a contributor.

### Model-facing skill catalog

Every turn, the chat service rebuilds a bounded skill catalog from the live
registry and injects it as a dynamic system instruction. The catalog lists each
visible skill's name, a one-line description, and its provider/source, framed
in `<available_skills>`. The catalog:

- Is rebuilt per turn, so a newly created skill appears on the next turn
  without any stored state to invalidate.
- Respects the model/user/admin visibility of each skill (admin-only skills
  appear only to principals with an admin role or `skills:admin` permission).
- Honors a character budget (`deepspace_skill_catalog_max_chars`, default
  12 000); entries are added in name order until the budget is reached. The
  rendered catalog states when it was truncated and provides the full digest so
  the model knows the list is partial.
- Includes a `source` metadata field on the system message with the catalog
  digest, truncation flag, and per-entry provenance (name, provider, source,
  `content_sha256`).
- Fails open: if a provider fails, the catalog is marked `incomplete` and the
  model sees a note that some skills could not be confirmed.

### Skill scope and delegation

A conversation may delegate to a subagent. The child must see the skills its
parent granted it and nothing else, even though both resolve the same workspace
roots. A scope is an immutable node in a chain: each node may narrow the set
it inherited, and a revoked name stays revoked.

The chain is deliberately explicit. A missing grant means "everything the
parent sees", which keeps the main agent behavior unchanged; an empty grant
means "nothing", which is how a child starts before its parent hands it a
skill.

When the parent creates a child with `subagent` or `subagent_fork`, the
`skill_allow` and `skill_deny` arguments build a new scope node that inherits
the parent's effective set and then narrows it. The child's durable record
stores the effective scope as a payload in its descriptor so a cold-resumed
child keeps the same visibility. The parent's scope is available on the
`ToolContext` as `skill_scope` and is carried into every tool call the child
makes, including `skill_list`, `skill_load`, `skill_resource`, and the
per-turn catalog injection.

A child that loads a skill or reads a resource is filtered by its scope at
lookup time, so it cannot reach an ungranted skill even if it guesses the name.

### Bundled Office skills and tools

The bundled Office definitions include `office`, `office-docx`, `office-xlsx`,
`office-pptx`, `office-render`, `office-recalculate`, `office-export-pdf`, and
the earlier `office_word`, `office_excel`, and `office_powerpoint` entries.
The manager also provides `badge_python`, `badge_security`, and
`badge_documentation`. The registry merges enabled definitions with the same
rank rules as filesystem skills; it does not overwrite project files to make
them available.

The `office_process` tool supports:

| Operation | Inputs | Result |
|---|---|---|
| `export_pdf` | Supported Word, spreadsheet, or presentation file | PDF written under the workspace |
| `recalculate_workbook` | `.xlsx` or `.ods` | Recalculated copy; source is preserved |
| `render_pages` | PDF or supported Office file | Up to 30 PNG page images in a workspace directory |

The tool rejects unsupported extensions, files larger than 100 MB, paths
outside the workspace, input overwrite, and invalid output extensions. It runs
LibreOffice and `pdftoppm` with a time limit, isolated temporary home/profile,
and a private input copy. `deepspace_bundled_office_enabled=false` removes the
Office skills and gates the native tool. The configured runtime manifest is
  preferred for binary paths. A packaged desktop runtime can use host-installed
  LibreOffice and Poppler when they are absent from the embedded manifest; an
  explicitly configured manifest remains restricted to its declared paths.
  Installation of LibreOffice and Poppler is a deployment responsibility.

### Settings

| Setting | Default | Effect |
|---|---|---|
| `deepspace_bundled_skills_enabled` | `true` | Includes bundled definitions in discovery. |
| `deepspace_bundled_office_enabled` | `true` | Includes Office instructions and enables `office_process`. |
| `deepspace_bundled_badge_enabled` | `true` | Includes quality-badge instructions. |
| `deepspace_skill_custom_roots` | empty | Adds trusted absolute roots to discovery. |
| `deepspace_skill_user_invocation_enabled` | `true` | Enables direct user slash/at-skill invocation. |
| `deepspace_skill_remote_url` | empty | Optional signed skill-catalog URL; must use HTTPS. |
| `deepspace_skill_remote_public_key` | empty | Ed25519 public key used to verify the remote catalog. |
| `deepspace_skill_enabled_names` | empty | Optional comma or semicolon separated skill allowlist. |
| `deepspace_skill_disabled_names` | empty | Optional comma or semicolon separated skill denylist. |
| `deepspace_skill_strict_names` | `false` | Require portable kebab-case filesystem skill names. |
| `deepspace_skill_catalog_enabled` | `true` | Enables per-turn model-facing catalog injection. |
| `deepspace_skill_catalog_max_chars` | `12000` | Character budget for the rendered catalog. |
| `deepspace_skill_cache_ttl_seconds` | `1.0` | Registry cache TTL; `0.0` disables caching (tests). |
| `deepspace_skill_telemetry_enabled` | `true` | Emits structured skill events to session telemetry. |

`deepspace_bundled_auto_install` and `deepspace_bundled_skill_dir` remain
legacy configuration fields; automatic file installation is not part of this
registry path. Discovery exposes definitions without writing them into a
workspace.

## What to watch for

- Custom roots are deployment trust boundaries. Use absolute paths owned by
  the deployment or user; each root is scanned on registry access.
- Skill instructions are prompt text, not executable code. Tools and package
  dependencies named in a skill must still be enabled and installed.
- A change event is emitted after a registry read or watcher poll detects a
  changed revision. Keep the unsubscribe callback for the listener lifecycle;
  the last unsubscribe stops that registry's polling thread.
- Office conversion fidelity depends on the installed LibreOffice version and
  source format. Verify the generated document before reporting success.
- Rendering requires `pdftoppm`; PDF rendering can work without LibreOffice,
  while rendering an Office source first requires LibreOffice conversion.
- Unit tests mock the Office executables to validate argument construction,
  workspace boundaries, and result handling. The native integration test
  `backend/platform-tests/office/test_real_office_runtime.py` also converts a
  generated DOCX, recalculates a generated XLSX, and renders the resulting PDF
  with installed LibreOffice and Poppler binaries. Conversion fidelity still
  depends on the installed versions and source format.
- The skill catalog is rebuilt every turn from the live registry. A large
  catalog or many providers increases per-turn latency; tune the budget or
  disable catalog injection if necessary.
- A child's skill scope is computed from the parent's effective scope at
  delegation time and stored in the child's descriptor. A cold-resumed child
  restores the same scope from that payload.
- The catalog injection is a dynamic system instruction (not persisted to the
  conversation history). The `source` metadata on the system message carries
  the catalog digest and truncation state but is not surfaced to the UI.

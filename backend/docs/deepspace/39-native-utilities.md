# 39. Native Utilities Package

DeepSpace now includes a complete native implementation of utility packages that were previously only available in the DeepSeek Harness (TypeScript). These utilities provide foundational building blocks for file operations, type safety, data structures, language detection, cryptography, and collections.

## Overview

| Module | DeepSeek Harness Package | Purpose |
|--------|-------------------------|---------|
| `atomic_write` | `@deepseek-ai/dsh-atomic-write` | Atomic file operations with cross-process locking |
| `brand` | `@deepseek-ai/dsh-brand` | Branded/nominal types for compile-time type safety |
| `chunked_list` | `@deepseek-ai/dsh-chunked-list` | Persistent append-only chunked lists with structural sharing |
| `code_language` | `@deepseek-ai/dsh-util-code-language` | File extension → syntax highlighting language mapping |
| `crypto` | `@deepseek-ai/dsh-util-crypto` | Browser-safe UUID and encoding utilities |
| `deque` | `@deepseek-ai/dsh-deque` | Circular deque with auto-compact |
| `output_retention` | `@deepseek-ai/dsh-output-retention` | Bounded item/text retention with omission accounting |
| `plugin_market_index` | `@deepseek-ai/dsh-plugin-market-index` | Validated plugin market metadata and pinned install targets |
| `project_manifest` | `@deepseek-ai/dsh-package-manifest` | Typed Python package metadata and localized fields |
| `native_command` | `@deepseek-ai/dsh-native-command` | Shell-free native commands and desktop path/application helpers |

The utility modules in this chapter are located in `app/deepspace/utils/`.
`chunked_list` uses Pydantic for recursive checkpoint validation;
`plugin_market_index` uses `semver`; and `project_manifest` uses Python's
`tomllib` (with a `tomli` fallback on Python versions before 3.11).

---

## 1. Atomic Write (`atomic_write.py`)

### Features
- **Atomic file replacement**: Write to temp file, then rename over target
- **Cross-process file locking**: `<file>.lock` sibling with PID-based stale lock takeover
- **Windows resilience**: Retries transient `EACCES`/`EBUSY`/`EPERM` errors
- **fsync durability**: Flushes file and parent directory on POSIX
- **Drop-in replacement**: Enhanced `atomic_write_json()` used by `workspace_registry.py`

### API
```python
from app.deepspace.utils.atomic_write import (
    write_file_atomic,
    write_json_atomic,
    atomic_write_json,      # Enhanced with locking
    with_file_lock,
    with_file_lock_async,
    FileLock,
    DEFAULT_LOCK_WAIT_MS,
)

# Basic atomic write
write_file_atomic("config.json", '{"key": "value"}', mode=0o644)
write_json_atomic("config.json", {"key": "value"}, indent=2)

# With cross-process locking (enhanced version used by workspace_registry)
atomic_write_json(Path("registry.json"), {"version": 2, "workspaces": []})

# Context manager for custom operations
with FileLock("data.json", wait_ms=5000):
    # Exclusive access to data.json
    data = json.loads(Path("data.json").read_text())
    data["counter"] += 1
    atomic_write_json(Path("data.json"), data)
```

### Lock Behavior
- Creates `<filename>.lock` with `O_CREAT | O_EXCL` (atomic exclusive create)
- Lock file contains holder PID + newline
- Stale locks (dead PID) are automatically taken over via claim-file protocol
- Windows: Retries unconfirmed `EPERM` once (holder may release between check and probe)
- Timeout configurable via `wait_ms` (default 2000ms, `None` = wait forever)

---

## 2. Branded Types (`brand.py`)

### Features
- **Compile-time nominal typing**: Prevents mixing semantically distinct identifiers
- **Runtime efficiency**: Frozen dataclasses with `__slots__`, zero overhead
- **30+ predefined types**: SessionId, ConversationId, AgentId, ToolCallId, etc.
- **JSON serializable**: With custom encoder
- **Immutable**: Frozen, cannot be modified after creation

### API
```python
from app.deepspace.utils.brand import (
    branded,                    # Factory function
    SessionId, ConversationId,  # Predefined types
    AgentId, ToolCallId,
    RunId, JobId, TenantId, UserId,
    WorkspaceId, ModelId, ProviderId,
    SecretId, FileId, AttachmentId,
    VectorId, EmbeddingId, RerankerId,
    CollectionId, DocumentId, ChunkId,
    EventId, LogId, TraceId, SpanId,
    RequestId, ResponseId, MessageId, ThreadId,
    ProcessId, PortNumber, TimeoutMs,
    TokenCount, ByteCount, Score, Probability,
    Latitude, Longitude,
    is_branded, get_brand_name, unbrand,
)

# Create custom branded type
UserId = branded("UserId", str)
ProjectId = branded("ProjectId", uuid.UUID)

# Usage
uid = UserId("user-123")
pid = ProjectId(uuid.uuid4())

# Compile-time safety (type checker catches this):
# uid = pid  # Error: UserId != ProjectId

# Runtime behavior
assert uid.value == "user-123"
assert str(uid) == "user-123"
assert uid == "user-123"  # Equality with raw value
assert hash(uid) == hash("user-123")

# Introspection
assert is_branded(uid, "UserId")
assert get_brand_name(uid) == "UserId"
assert unbrand(uid) == "user-123"

# JSON serialization (requires custom encoder)
import json
class BrandedEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, _BrandedBase):
            return obj.value
        return super().default(obj)

json.dumps({"user_id": uid}, cls=BrandedEncoder)  # '{"user_id": "user-123"}'
```

### Predefined Types by Category
| Category | Types |
|----------|-------|
| Core IDs | SessionId, ConversationId, AgentId, ToolCallId, RunId, JobId |
| Auth | TenantId, UserId |
| Workspace | WorkspaceId, ProjectId (custom) |
| Models | ModelId, ProviderId, SecretId |
| Storage | FileId, AttachmentId, VectorId, EmbeddingId, RerankerId, CollectionId, DocumentId, ChunkId |
| Events | EventId, LogId, TraceId, SpanId |
| Requests | RequestId, ResponseId, MessageId, ThreadId |
| System | ProcessId (int), PortNumber (int), TimeoutMs (int) |
| Metrics | TokenCount (int), ByteCount (int), Score (float), Probability (float) |
| Geo | Latitude (float), Longitude (float) |

---

## 3. Chunked List (`chunked_list.py`)

### Features
- **Persistent data structure**: Append creates new head, shares unchanged chunks
- **Structural sharing**: O(1) amortized append, O(N/64) space for iteration
- **Bounded chunks**: 64 items per chunk (configurable via `CHUNK_CAPACITY`)
- **Pydantic validation**: Schema for checkpoint serialization
- **Full sequence protocol**: Indexing, iteration, len, reversed, negative indices

### API
```python
from app.deepspace.utils.chunked_list import (
    CHUNK_CAPACITY,           # 64
    ChunkedList,              # Generic dataclass
    append_chunked_list,      # Append with sharing
    prepend_chunked_list,     # Prepend (less common)
    iterate_chunked_list,     # Iterator (insertion order)
    to_list,                  # Convert to list
    from_list,                # Build from list
    chunked_list_len,         # Length
    chunked_list_is_empty,    # Empty check
    chunked_list_get,         # Safe get (returns None)
    chunked_list_schema,      # Pydantic schema factory
    validate_chunked_list,    # Runtime validation
)

# Build a list
head: ChunkedList[str] | None = None
for item in ["a", "b", "c"]:
    head = append_chunked_list(head, item)

# Iterate (insertion order: oldest first)
for item in iterate_chunked_list(head):
    print(item)  # "a", "b", "c"

# Indexing (supports negative indices)
head[0]   # "a"
head[-1]  # "c"

# Convert to/from regular lists
regular = to_list(head)          # ["a", "b", "c"]
head2 = from_list(["x", "y", "z"])

# Pydantic validation for checkpoints
schema = chunked_list_schema(str)
data = {"values": ["c", "d"], "previous": {"values": ["a", "b"], "previous": None}}
validated = schema.model_validate(data)

# Validate existing chunked list
validated_head = validate_chunked_list(head, str)
```

### ChunkedList Dataclass
```python
@dataclass(frozen=True, slots=True)
class ChunkedList(Generic[T]):
    values: tuple[T, ...]           # 1-64 items
    previous: "ChunkedList[T] | None" = None  # Older chunk

    # Properties
    def __len__(self) -> int:       # Total items
    def __getitem__(self, i: int) -> T:  # Indexing
    def __iter__(self) -> Iterator[T]:   # Iteration
    def __reversed__(self) -> Iterator[T]:
```

---

## 4. Code Language (`code_language.py`)

### Features
- **92 languages** with canonical IDs and extensions
- **Single source of truth** for syntax highlighting across read tool, preview, diff, LSP
- **Persisted `lang` hints** for read card with override table
- **LSP config generation** with dot-prefixed extensions
- **Categorized extension sets** for tool filtering

### API
```python
from app.deepspace.utils.code_language import (
    language_for_path,           # "main.py" -> "python"
    read_lang_hint_for_path,     # "main.py" -> "py"
    CODE_HIGHLIGHT_EXTENSIONS,   # All recognized extensions (tuple)
    EXTENSION_TO_LANGUAGE,       # ext -> canonical language
    LANGUAGE_EXTENSIONS,         # language -> extensions
    SHORT_BY_LANGUAGE,           # language -> short hint
    READ_LANG_BY_EXTENSION,      # Override extensions
    get_language_extensions,     # Get extensions for language
    is_recognized_extension,     # Check extension
    get_all_languages,           # All canonical languages
    build_lsp_extension_map,     # LSP config dict
    COMMON_CONFIG_EXTENSIONS,    # Config file extensions
    COMMON_SOURCE_EXTENSIONS,    # Source code extensions
    COMMON_MARKUP_EXTENSIONS,    # Markup extensions
)

# Language detection
language_for_path("src/main.py")           # "python"
language_for_path("component.tsx")         # "typescript"
language_for_path("config.json")           # "json"
language_for_path("C:\\path\\main.rs")     # "rust" (Windows paths)
language_for_path(".env")                  # "dotenv"
language_for_path("Makefile")              # None (not in table)

# Read card persisted hints
read_lang_hint_for_path("main.py")         # "py"
read_lang_hint_for_path("component.tsx")   # "tsx" (override)
read_lang_hint_for_path("main.tf")         # "tf" (HCL override)
read_lang_hint_for_path("build.gradle")    # "gradle" (Groovy override)

# LSP configuration
lsp_map = build_lsp_extension_map()
# {".py": "python", ".rs": "rust", ".ts": "typescript", ...}

# Extension sets for filtering
"json" in COMMON_CONFIG_EXTENSIONS     # True
"py" in COMMON_SOURCE_EXTENSIONS       # True
"md" in COMMON_MARKUP_EXTENSIONS       # True
```

### Override Extensions (`READ_LANG_BY_EXTENSION`)
| Extension | Language | Persisted Hint |
|-----------|----------|----------------|
| `tsx`, `jsx` | typescript/javascript | `tsx`, `jsx` |
| `tf`, `tfvars` | hcl | `tf`, `tfvars` |
| `gradle` | groovy | `gradle` |
| `ipynb` | json | `ipynb` |
| `jsonc`, `jsonl`, `ndjson` | json | same |
| `pyi` | python | `pyi` |
| `mts`, `cts` | typescript | same |
| `mjs`, `cjs` | javascript | same |

---

## 5. Crypto (`crypto.py`)

### Features
- **RFC 9562 UUIDv4**: Cryptographically secure via `secrets.token_bytes()`
- **Base64/URL encoding**: Standard and URL-safe variants
- **Secure token generation**: Base64URL and hex
- **Constant-time comparison**: Timing attack resistant
- **UUIDv7 support**: Timestamp-ordered (falls back to v4)

### API
```python
from app.deepspace.utils.crypto import (
    random_uuid,              # RFC 9562 UUIDv4 string
    uuid_to_base64,           # UUID -> base64url (22 chars)
    bytes_to_base64,          # bytes -> base64
    bytes_to_base64url,       # bytes -> base64url (no padding)
    base64_to_bytes,          # base64 -> bytes (validates)
    base64url_to_bytes,       # base64url -> bytes (validates)
    generate_secure_token,    # Secure base64url token
    generate_secure_hex,      # Secure hex token
    constant_time_compare,    # Timing-safe compare
    generate_uuid7,           # UUIDv7 or v4 fallback
    uuid4, new_id, new_token, new_hex,  # Convenience aliases
)

# UUID generation
uid = random_uuid()                    # "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx"
assert uuid.UUID(uid).version == 4

# Base64URL encoding (compact)
b64 = uuid_to_base64(uid)              # 22 chars, no padding
# Round-trip
import base64
decoded = base64.urlsafe_b64decode(b64 + "==")
assert str(uuid.UUID(bytes=decoded)) == uid

# Secure tokens
token = generate_secure_token()        # 43 chars (32 bytes)
token = generate_secure_token(16)      # 22 chars (16 bytes)
hex_token = generate_secure_hex()      # 64 chars (32 bytes)

# Constant-time comparison
assert constant_time_compare("secret", "secret")
assert not constant_time_compare("secret", "different")

# UUIDv7 (timestamp-ordered)
uid7 = generate_uuid7()                # v7 if uuid6 available, else v4
```

### Format Details
- `random_uuid()`: Standard UUID format with hyphens (36 chars)
- `uuid_to_base64()`: Base64URL without padding (22 chars for 16 bytes)
- `generate_secure_token(n)`: `4/3 * n` chars rounded up, no padding

---

## 6. Deque (`deque.py`)

### Features
- **Circular buffer**: Amortized O(1) push/pop at both ends
- **Auto-compact**: Shrinks when size ≤ capacity/4 (min 16)
- **Explicit clearing**: Removed entries set to `None` for GC
- **Full sequence protocol**: Indexing, iteration, len, reversed
- **Bounded variant**: `DequeWithMaxlen` drops oldest when full

### API
```python
from app.deepspace.utils.deque import (
    Deque,                    # Unbounded circular deque
    DequeWithMaxlen,          # Bounded variant
    MIN_CAPACITY,             # 16
)

# Basic usage
dq = Deque[int]()
dq.push_back(1)
dq.push_back(2)
dq.push_front(0)

assert dq.pop_front() == 0
assert dq.pop_back() == 2

# Iteration
dq = Deque([1, 2, 3])
list(dq)          # [1, 2, 3]
list(reversed(dq)) # [3, 2, 1]

# Indexing
dq[0]   # 1 (front)
dq[-1]  # 3 (back)
dq[0] = 100  # Mutation

# Capacity management
dq = Deque[int]()
for i in range(100):
    dq.push_back(i)
# Buffer grows as needed

# Auto-compact
for _ in range(95):
    dq.pop_front()
# Buffer shrinks to min capacity

# Conversion
dq.to_list()      # [5, 6, 7, 8, 9]
dq.extend([10, 11])
dq.extendleft([3, 2])  # Adds in order: 3, 2
dq.rotate(1)     # [11, 5, 6, 7, 8, 9, 10, 3, 2]
dq.rotate(-1)    # Back to original

# Initialization
Deque([1, 2, 3])
Deque((1, 2, 3))
Deque(x for x in range(3))

# Bounded deque
dq = DequeWithMaxlen[int](maxlen=3)
dq.push_back(1)
dq.push_back(2)
dq.push_back(3)
dq.push_back(4)   # Drops 1
assert list(dq) == [2, 3, 4]

DequeWithMaxlen([1,2,3,4,5], maxlen=3)  # [3, 4, 5]
```

## 7. Output Retention (`output_retention.py`)

`ItemRetainer` and `TextRetainer` bound ordered tool output. Text modes retain
the head, tail, or both ends; truncation records exact omitted byte counts and
preserves UTF-8 boundaries. `truncate_without_splitting_surrogate_pair`
measures its cap in UTF-16 code units so it matches the shared client contract.
Use the retention result when presenting tool output so the model can tell
whether it saw the complete result.

## 8. Plugin Market Index (`plugin_market_index.py`)

`parse_market_index()` validates format-1 metadata, locale identifiers,
package names, semver, source URLs, commit pins, and safe package paths.
`market_install_target()` accepts a validated version or its JSON mapping and
returns an exact npm or git install target. It does not download or install a
plugin; plugin installation still goes through the authorized plugin service.

## 9. Project Manifest (`project_manifest.py`)

`load_manifest_from_file()` reads TOML and validates the package identity,
dependencies, PEP 621 fields, and DeepSpace bundle/profile/client settings.
`resolve_localized()` selects the requested locale, then English, then the
first available value; an empty locale map resolves to an empty string.

## 10. Native Command (`native_command.py`)

`NativeCommandRunner` runs argument vectors without a shell and reports
timeouts, captured output, and exit status. The path-opening helpers support
the host's default applications and Linux desktop entries. Use the shared
`subprocess_stdio` launcher for contained application subprocesses; native
openers are a deliberate OS integration boundary and are not a sandbox.

---

## Integration Status

### Updated Modules
- **`workspace_registry.py`**: Now uses enhanced `atomic_write_json()` with cross-process locking from `app.deepspace.utils.atomic_write`
- **`__init__.py`**: Exports all utilities from `app.deepspace.utils`

### Import Patterns
```python
# Individual modules
from app.deepspace.utils.atomic_write import atomic_write_json, FileLock
from app.deepspace.utils.brand import SessionId, branded
from app.deepspace.utils.chunked_list import ChunkedList, append_chunked_list
from app.deepspace.utils.code_language import language_for_path, read_lang_hint_for_path
from app.deepspace.utils.crypto import random_uuid, generate_secure_token
from app.deepspace.utils.deque import Deque, DequeWithMaxlen

# Or bulk import
from app.deepspace.utils import (
    atomic_write_json, SessionId, append_chunked_list,
    language_for_path, random_uuid, Deque,
)
```

---

## Testing

The isolated utility/native-runtime run on 2026-11-12 covered the focused
pytest modules for atomic writes, chunked lists, code-language detection,
deque behavior, output retention, plugin-market metadata, project manifests,
native commands, Windows runtime seams, and computer-use subprocesses. It
completed with **289 passed, 0 failed**, and two existing Pydantic deprecation
warnings. Windows-specific behavior in this Linux run is exercised through
platform mocks; it does not replace a native Windows run.

Run the same focused set from the repository root with:

```bash
./backend/scripts/test-isolated.sh -m unit_no_db -n 0 --tb=short \
  tests/unit/test_deepspace_utils_atomic_write.py \
  tests/unit/test_deepspace_utils_chunked_list.py \
  tests/unit/test_deepspace_utils_code_language.py \
  tests/unit/test_deepspace_utils_deque.py \
  tests/unit/test_deepspace_utils_output_retention.py \
  tests/unit/test_deepspace_utils_plugin_market_index.py \
  tests/unit/test_deepspace_utils_project_manifest.py \
  tests/unit/test_deepspace_utils_native_command.py \
  tests/unit/test_deepspace_windows_native_runtime.py \
  tests/unit/test_deepspace_computer_use_subprocess.py
```

---

## Migration from DeepSeek Harness

| Harness (TypeScript) | DeepSpace (Python) | Notes |
|---------------------|-------------------|-------|
| `dsh-atomic-write` | `atomic_write.py` | Enhanced with locking, Windows retries |
| `dsh-brand` | `brand.py` | Dataclass-based, 30+ predefined types |
| `dsh-chunked-list` | `chunked_list.py` | Pydantic validation, negative indexing |
| `dsh-util-code-language` | `code_language.py` | 92 languages, read card hints, LSP map |
| `dsh-util-crypto` | `crypto.py` | RFC 9562 UUID, base64url, constant-time |
| `dsh-deque` | `deque.py` | Auto-compact, bounded variant |

All six modules are **native Python implementations**. They use the standard library, plus Pydantic for `chunked_list` schema validation. Later utility batches add third-party dependencies (`croniter`, `python-dateutil`, `apscheduler`, `semver`, and `tomli`); those are not part of this chapter's six modules.

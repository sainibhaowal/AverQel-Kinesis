from __future__ import annotations

import base64
import ipaddress
import json
from functools import lru_cache
from typing import Final, Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENV_ALLOWED: Final[set[str]] = {"development", "staging", "production", "test"}
PRODUCTION_ENVS: Final[set[str]] = {"production"}

DEFAULT_JWT_SECRET: Final[str] = "change-me-please-use-env-secret-min-32-chars"
DEFAULT_REFRESH_SECRET: Final[str] = "change-me-refresh-hash-secret-min-32-chars"
DEFAULT_DATABASE_URL: Final[str] = "sqlite:///./data/knowledge.db"
DEFAULT_MINIO_ACCESS_KEY: Final[str] = "minioadmin"
DEFAULT_MINIO_SECRET_KEY: Final[str] = "minioadmin"
#: The wall-clock budget one tool call may take when the deployment names none.
DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS: Final[float] = 120.0

REMOTE_LLM_PROVIDERS: Final[set[str]] = {
    "openai",
    "deepseek",
    "larprouter",
    "groq",
    "groq-openai-compatible",
    "mistral",
    "meta",
    "cline",
    "together",
    "fireworks",
    "perplexity",
}

# ---------------------------------------------------------------------------
# LLM Provider Presets
# ---------------------------------------------------------------------------

LLM_PROVIDER_PRESETS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "larprouter": ("https://larprouter.com/v1", "deepseek-v4.1-flash"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "groq-openai-compatible": (
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
    ),
    "mistral": ("https://api.mistral.ai/v1", "mistral-large-latest"),
    "meta": ("https://api.meta.ai/v1", "muse-spark-1.3"),
    "cline": ("https://api.cline.bot/api/v1", ""),
    "together": (
        "https://api.together.xyz/v1",
        "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
    ),
    "fireworks": (
        "https://api.fireworks.ai/inference/v1",
        "accounts/fireworks/models/llama-v3p1-70b-instruct",
    ),
    "perplexity": ("https://api.perplexity.ai", "llama-3.1-sonar-large-128k-online"),
    "ollama": ("http://localhost:11434/v1", "llama3"),
    "vllm": ("http://localhost:8000/v1", "default"),
    "lmstudio": ("http://localhost:1234/v1", "default"),
    "custom": ("", ""),
}

# Only providers known to be local-by-default belong here.
LOCAL_LLM_PROVIDERS: Final[set[str]] = {"ollama", "vllm", "lmstudio"}

# ---------------------------------------------------------------------------
# Helper Normalizers
# ---------------------------------------------------------------------------


def _normalize_str_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []

    for item in values:
        cleaned = item.strip()
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)

    return normalized


def _normalize_ext_list(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    for item in values:
        cleaned = item.strip().lower()
        if not cleaned:
            continue
        if not cleaned.startswith("."):
            cleaned = f".{cleaned}"
        if cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)

    return normalized


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_valid_origin(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.path.rstrip("/")
    )


def _is_local_like_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "0.0.0.0"}  # nosec B104


def _is_private_service_host(value: str) -> bool:
    parsed = urlparse(value if "://" in value else f"http://{value}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {
        "localhost",
        "127.0.0.1",
        "::1",
        "host.docker.internal",
    }:
        return True
    if "." not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_private or address.is_unspecified
    except ValueError:
        return False


def _parse_aes_gcm_keyring(
    raw: str | None, *, setting_name: str, key_label: str
) -> dict[str, bytes]:
    if raw is None:
        return {}

    cleaned = raw.strip()
    if not cleaned:
        return {}

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{setting_name} must be valid JSON") from exc

    if not isinstance(parsed, dict) or not parsed:
        raise ValueError(f"{setting_name} must be a non-empty JSON object")

    keyring: dict[str, bytes] = {}
    for kid, encoded_key in parsed.items():
        if not isinstance(kid, str) or not kid.strip():
            raise ValueError(f"{key_label} key ids must be non-empty strings")
        if not isinstance(encoded_key, str) or not encoded_key.strip():
            raise ValueError(f"{key_label} keys must be non-empty base64 strings")
        try:
            key_bytes = base64.urlsafe_b64decode(encoded_key.encode("utf-8"))
        except Exception as exc:  # pragma: no cover - defensive branch
            raise ValueError(f"{key_label} key for kid={kid!r} is not valid base64") from exc
        if len(key_bytes) not in {16, 24, 32}:
            raise ValueError(f"{key_label} keys must decode to 16, 24, or 32 bytes for AES-GCM")
        keyring[kid.strip()] = key_bytes
    return keyring


def _parse_provider_secret_keyring(raw: str | None) -> dict[str, bytes]:
    return _parse_aes_gcm_keyring(
        raw, setting_name="provider_secret_keyring_json", key_label="provider secret"
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AKS_",
        case_sensitive=False,
        extra="ignore",
    )

    # -----------------------------------------------------------------------
    # App
    # -----------------------------------------------------------------------

    app_name: str = "AverQel"
    # Release metadata is injected by the deployment pipeline. A missing
    # value must be visibly non-release rather than falsely reporting v1.0.0.
    app_version: str = Field(default="development", validation_alias="AKS_APP_VERSION")
    release_version: str = Field(default="development", validation_alias="AKS_RELEASE_VERSION")
    git_sha: str = Field(default="unknown", validation_alias="AKS_GIT_SHA")
    build_timestamp_utc: str | None = Field(default=None, validation_alias="AKS_BUILD_TIMESTAMP")
    env: str = "development"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"

    # -----------------------------------------------------------------------
    # Core Infrastructure
    # -----------------------------------------------------------------------

    database_url: str = DEFAULT_DATABASE_URL
    database_pool_size: int = 8
    database_max_overflow: int = 4
    database_pool_timeout_seconds: float = 4.0
    database_statement_timeout_seconds: float = 15.0
    database_lock_timeout_seconds: float = 3.0
    redis_url: str = "memory://"
    storage_backend: str = "local"
    local_storage_path: str = "./data/storage"

    @model_validator(mode="after")
    def _resolve_averqel_home_defaults(self) -> Settings:
        """Upgrade legacy ``./data/`` defaults to the unified ``.averqel`` home.

        This runs after pydantic-settings has applied env-var overrides, so any
        explicitly configured value (e.g. a Docker Postgres URL or MinIO path)
        is never touched.  Only the two fields that still hold their factory
        defaults are upgraded, ensuring every process entry point — ``uvicorn``,
        test runners, and standalone scripts — resolves the same location without
        requiring manual env-var injection.
        """
        # Inline import avoids a module-level circular dependency: config.py is
        # imported early; averqel_home.py depends only on stdlib.
        from app.core.averqel_home import averqel_storage_path, sqlite_url

        if self.database_url == DEFAULT_DATABASE_URL:
            self.database_url = sqlite_url("knowledge.db")
        if self.local_storage_path == "./data/storage":
            self.local_storage_path = str(averqel_storage_path())
        return self

    # The typed session log's payload column is encrypted with AES-GCM.  A
    # deployment can name a keyring and its active key id, or let the local
    # key file be created under the AverQel home directory.
    deepspace_session_log_encryption: bool = True
    deepspace_session_log_active_kid: str | None = None
    deepspace_session_log_keyring_json: str | None = None

    # DeepSpace deterministic web-research controls. Browser rendering is
    # disabled until an isolated renderer is explicitly deployed.
    # The local app manages a loopback SearXNG runtime; container deployments
    # override this with the service hostname in their environment.
    searxng_base_url: str = "http://127.0.0.1:8888"
    searxng_local_runtime_enabled: bool = False
    searxng_runtime_dir: str | None = None
    searxng_source_url: str = "https://github.com/searxng/searxng.git"
    searxng_source_ref: str = "master"
    searxng_install_timeout_seconds: int = Field(default=900, ge=60, le=3600)
    searxng_start_timeout_seconds: int = Field(default=90, ge=10, le=300)
    deepspace_research_enabled: bool = True
    deepspace_research_browser_enabled: bool = False
    deepspace_research_browser_url: str | None = None
    deepspace_research_browser_token: str | None = None
    deepspace_url_read_timeout_seconds: int = Field(default=15, ge=5, le=30)
    deepspace_url_read_max_bytes: int = Field(default=2_000_000, ge=16_384, le=2_000_000)
    # Model-facing web content must have a character ceiling in addition to
    # the transport byte ceiling, otherwise a large page can consume the
    # remaining context window in one tool result.
    deepspace_url_read_max_chars: int = Field(default=48_000, ge=4_000, le=200_000)
    deepspace_url_allowed_domains: list[str] = Field(default_factory=list)
    # Sandboxed code/data execution is off unless a separately isolated
    # executor has been deployed and authenticated.  It is never run in the
    # API or worker container.
    deepspace_sandbox_enabled: bool = True
    deepspace_sandbox_url: str | None = "http://127.0.0.1:3020"
    deepspace_sandbox_token: str | None = "local-sandbox-e2e-token"
    deepspace_sandbox_timeout_seconds: int = Field(default=20, ge=1, le=30)

    # The project workspace the file system and shell tools operate in.  One
    # directory per tenant and user lives under this base, and every path a
    # tool resolves is checked against it.  An empty value uses
    # ``<local_storage_path>/workspaces``.
    deepspace_workspace_root: str | None = None

    # The local shell tools.  They run a real command in the worker process,
    # so the permission mode decides how much of the host the command may
    # reach.  ``read-only`` refuses commands that plainly mutate state,
    # ``workspace-write`` is the default and starts every command inside the
    # workspace, and ``danger-full-access`` removes the checks.
    deepspace_shell_enabled: bool = True
    deepspace_permission_mode: Literal["read-only", "workspace-write", "danger-full-access"] = (
        "workspace-write"
    )
    deepspace_shell_timeout_seconds: int = Field(default=120, ge=1, le=900)
    deepspace_shell_max_output_bytes: int = Field(default=1_000_000, ge=1_024, le=20_000_000)

    # Delegation limits for the subagent tools.  The depth is the number of
    # nested delegations a root agent may start; the child cap bounds how
    # many children one conversation may hold at once.  Depth 3 lets a root
    # delegate to a child that delegates once more; every level still obeys
    # the same child cap.
    deepspace_subagent_max_depth: int = Field(default=3, ge=0, le=8)
    deepspace_subagent_max_children: int = Field(default=16, ge=1, le=64)

    # Native write tools can pause for a human decision before they run.
    # ``ask`` records a pending approval and resumes the turn after the user
    # answers; ``never`` keeps unattended execution.
    deepspace_approval_mode: Literal["ask", "never"] = "never"
    # The containment policy for the local shell tools.  It is separate from
    # the approval mode so a read-only deployment can still refuse every
    # mutating command without asking.
    deepspace_sandbox_policy: Literal["read-only", "workspace-write", "danger-full-access"] = (
        "workspace-write"
    )
    # The operating-system cage for ``bash`` and ``pwsh``.  ``auto`` selects
    # bubblewrap on Linux, Landlock when namespaces are unavailable, the
    # seatbelt sandbox on macOS, the job-object runner on Windows, and
    # Docker when a sandbox image is configured.  ``none`` keeps inspection
    # only.
    deepspace_sandbox_backend: Literal[
        "auto", "bwrap", "landlock", "sandbox-exec", "docker", "windows", "none"
    ] = "auto"
    # Keep network access inside the cage.  Local builds and package installs
    # need it; a deployment that runs untrusted code can turn it off.
    deepspace_sandbox_network: bool = True
    # The container image the Docker backend runs.  Docker is skipped when
    # this is empty, so no image is pulled without an explicit choice.
    deepspace_sandbox_image: str | None = None
    # Refuse a shell command when the deployment demands a cage but no
    # backend is available.  Off by default so an unsupported host still
    # works with inspection only.
    deepspace_sandbox_required: bool = False

    # The character ceiling above which a stored tool result is pruned from
    # the request history and replaced by an excerpt plus a retrievable
    # reference.
    deepspace_tool_result_prune_threshold: int = Field(default=8192, ge=1024, le=200_000)

    # The largest number of tool calls one provider round may run at the same
    # time.  Independent read calls share this budget; write calls stay
    # serialised behind the write lock.
    deepspace_max_concurrent_tools: int = Field(default=8, ge=1, le=32)

    # The wall-clock budget one tool call may take.  Every native tool call
    # runs under a deadline, so a hung tool cannot stall a turn.  A tool that
    # declares its own deadline uses that one instead of this budget.
    deepspace_tool_timeout_seconds: float = Field(
        default=DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS, gt=0.0, le=3600.0
    )

    # The character ceiling above which a tool result is spilled to durable,
    # content-addressed storage instead of being carried in the model context.
    # The model receives a preview plus a reference it can read back.
    deepspace_spill_threshold_chars: int = Field(default=24_000, ge=2_000, le=500_000)
    # The largest spilled result one spill store accepts, in characters.
    deepspace_spill_max_chars: int = Field(default=2_000_000, ge=10_000, le=20_000_000)

    # Programmatic tool calling.  The model writes one program that calls
    # host tools as ordinary functions, and the program runs in a confined
    # child process.
    deepspace_ptc_enabled: bool = True
    deepspace_ptc_timeout_seconds: int = Field(default=60, ge=5, le=300)
    # The largest number of host tool calls one program may make.
    deepspace_ptc_max_tool_calls: int = Field(default=24, ge=1, le=128)

    # The largest shell output that is kept. A larger output is spilled to
    # durable storage and read back in pages, so nothing is discarded.
    deepspace_shell_capture_bytes: int = Field(default=8_000_000, ge=65_536, le=64_000_000)

    # The dedicated plan mode.  While a conversation is in plan mode the
    # runtime offers read-only tools plus the plan submission tool, and the
    # plan is reviewed before any work starts.
    deepspace_plan_mode_enabled: bool = True

    # The agent presets a conversation may run in.  A preset says which
    # tools the agent has, whether programmatic tool calling is on, how
    # deep the agent may delegate, and which sandbox mode it starts in.
    deepspace_default_preset: str = "standard"

    # The reasoning effort a conversation requests when its preset names
    # none.  The first setting that carries a value wins, so a deployment may
    # name it either way.  The three values are the ones the preset
    # catalogue knows.
    deepspace_reasoning_effort: Literal["low", "medium", "high"] | None = None
    deepspace_default_reasoning_effort: Literal["low", "medium", "high"] | None = None

    # The loop guard.  It stops an agent that calls the same tool with the
    # same arguments over and over, or that runs past its turn budget.
    deepspace_loop_guard_enabled: bool = True
    deepspace_loop_guard_max_tool_calls: int = Field(default=64, ge=1, le=1_000)
    deepspace_loop_guard_max_identical_calls: int = Field(default=4, ge=1, le=100)
    deepspace_loop_guard_max_repeated_arguments: int = Field(default=6, ge=1, le=200)
    deepspace_loop_guard_max_turn_seconds: float = Field(default=900.0, ge=30.0, le=7_200.0)

    # The wall-clock budget one agent turn may take.  The runtime may extend
    # the deadline a bounded number of times, saving a checkpoint each time,
    # so this is the length of one attempt rather than of the whole run.
    deepspace_agent_max_runtime_seconds: int = Field(default=1_800, ge=30, le=86_400)
    # The number of agent steps one run keeps.  The oldest steps are pruned
    # from the run, so the number is the size of the replayable history.
    deepspace_agent_retained_steps: int = Field(default=10_000, ge=100, le=10_000)

    # The model a conversation runs on.  "auto" lets DeepSpace choose the
    # model that fits the work; "manual" uses the model the user picked.
    deepspace_model_selection_mode: Literal["auto", "manual"] = "auto"
    deepspace_model_auto_choice_enabled: bool = True

    # The hook manifest the deployment runs. A hook may watch every tool
    # call and refuse one.
    deepspace_hook_manifest: str = ""

    # The tool hooks, in the Claude Code and Codex dialect.
    deepspace_hook_claude_dialect: bool = True
    deepspace_hook_audit_enabled: bool = True

    # The remote hosts DeepSpace may reach over SSH.  The private keys live
    # in this directory; the database holds only the name of a key.
    deepspace_ssh_key_directory: str = ""

    # The remote runtime: the whole tool surface on a remote host.
    deepspace_ssh_runtime_enabled: bool = True

    # The named teammates that work the same task.
    deepspace_agent_team_enabled: bool = True
    deepspace_agent_team_max_members: int = Field(default=8, ge=2, le=32)
    # A running teammate whose durable worker heartbeat is older than this is
    # released and its queued work can be dispatched again.
    deepspace_agent_team_lease_seconds: int = Field(default=1_800, ge=60, le=86_400)

    # Whether the deployment lets an extension run code.
    deepspace_plugin_code_enabled: bool = True

    # The plugin system.  A plugin adds tools, guidance, and tool
    # restrictions to the agent without changing the product.  The
    # deployment's own plugin directory is read in addition to the
    # built-in plugins.
    deepspace_plugins_enabled: bool = True
    deepspace_plugin_directory: str = ""

    # The persistent terminal sessions a conversation may hold.  A session
    # keeps a shell alive between tool calls, so the agent can run an
    # interactive program or keep a process running.
    deepspace_terminal_enabled: bool = True
    deepspace_terminal_max_sessions_per_conversation: int = Field(default=4, ge=1, le=32)
    deepspace_terminal_max_sessions: int = Field(default=32, ge=1, le=256)
    deepspace_terminal_idle_timeout_seconds: float = Field(default=900.0, ge=30.0, le=86_400.0)
    # The number of output bytes one terminal session keeps.  A session that
    # writes more drops its oldest bytes, so the number is the size of the
    # output a read can still return.
    deepspace_terminal_max_output_bytes: int = Field(default=262_144, ge=1_024, le=20_000_000)

    # The in-app browser.  The runtime drives a real Chromium over the DevTools
    # protocol, so a deployment that turns it on needs a browser on the host
    # and accepts the memory a browser process costs.  The browser is off
    # until a deployment turns it on; the local single-machine deployment
    # turns it on explicitly.
    deepspace_browser_enabled: bool = False
    # The path of the Chromium binary the browser runtime starts.  An empty
    # value makes the runtime look for a browser in the usual places and
    # report clearly when the host has none.
    deepspace_browser_executable: str = ""
    deepspace_browser_idle_timeout_seconds: float = Field(default=900.0, ge=30.0, le=86_400.0)

    # Workspace instruction loader (AGENTS.md/CLAUDE.md hierarchy).
    # Loads baseline and nested instruction files and injects them into the
    # system prompt before each turn.
    deepspace_workspace_instructions_enabled: bool = True
    deepspace_workspace_instructions_max_bytes: int = Field(default=32_000, ge=4_000, le=200_000)
    deepspace_workspace_instructions_refresh_interval_seconds: int = Field(
        default=300, ge=30, le=3600
    )

    # Per-step time context.  Injects current time and elapsed time into the
    # request history at configurable intervals.
    deepspace_time_context_enabled: bool = True
    deepspace_time_context_time_zone: str = "UTC"
    deepspace_time_context_refresh_interval_ms: int = Field(default=0, ge=0, le=3_600_000)

    # Language Server Protocol (LSP) navigation.
    # Configure language servers for goToDefinition, findReferences, etc.
    deepspace_lsp_enabled: bool = False
    deepspace_lsp_servers: dict[str, dict] = Field(default_factory=dict)
    deepspace_lsp_tool_timeout_ms: int = Field(default=60_000, ge=5_000, le=300_000)
    deepspace_lsp_max_locations: int = Field(default=100, ge=10, le=500)
    deepspace_lsp_max_result_chars: int = Field(default=16_000, ge=4_000, le=100_000)

    # Node.js PTC runtime.
    # Execute JavaScript/TypeScript programs with host tool bindings.
    deepspace_ptc_node_enabled: bool = False
    deepspace_ptc_node_executable: str = "node"
    deepspace_ptc_node_bootstrap_path: str | None = None
    deepspace_ptc_node_timeout_ms: int = Field(default=120_000, ge=5_000, le=600_000)
    deepspace_ptc_node_max_timeout_ms: int = Field(default=600_000, ge=5_000, le=3_600_000)
    deepspace_ptc_node_max_output_bytes: int = Field(
        default=67_108_864, ge=1_000_000, le=500_000_000
    )
    deepspace_ptc_node_max_old_generation_mb: int = Field(default=512, ge=128, le=4096)
    deepspace_ptc_node_max_message_bytes: int = Field(
        default=134_217_728, ge=1_000_000, le=1_000_000_000
    )
    deepspace_ptc_node_max_pending_calls: int = Field(default=128, ge=1, le=1000)
    deepspace_ptc_node_grace_ms: int = Field(default=3_000, ge=100, le=60_000)

    # File and session references.
    # @file and @session completion and resolution.
    deepspace_file_references_enabled: bool = True
    deepspace_file_references_max_candidates: int = Field(default=50, ge=10, le=200)
    deepspace_session_references_enabled: bool = True
    deepspace_session_references_max_bytes: int = Field(default=50_000, ge=10_000, le=500_000)
    deepspace_session_references_max_refs: int = Field(default=5, ge=1, le=20)

    # Goal round driver.
    # Automatic goal continuation with round limits and race fences.
    deepspace_goal_driver_enabled: bool = True
    deepspace_goal_max_rounds_default: int = Field(default=3, ge=1, le=10)

    # Session title generation.
    # LLM-generated, auto-updating session titles.
    deepspace_session_title_enabled: bool = True
    deepspace_session_title_provider: Literal[
        "first-prompt", "all-prompts-llm", "first-prompt-llm"
    ] = "first-prompt"
    deepspace_session_title_model: str | None = None
    deepspace_session_title_max_chars: int = Field(default=80, ge=20, le=200)
    deepspace_session_title_refresh_turns: int = Field(default=3, ge=1, le=20)

    # Compaction pipeline.
    # LLM-based summarization, manual /compact command, and compaction events.
    deepspace_compaction_llm_enabled: bool = False
    deepspace_compaction_llm_model: str | None = None
    deepspace_compaction_llm_max_tokens: int = Field(default=4000, ge=500, le=32000)
    deepspace_compaction_manual_enabled: bool = True

    # Session log export.
    # JSONL/JSON download of session events.
    deepspace_session_export_enabled: bool = True
    deepspace_session_export_max_events: int = Field(default=10000, ge=100, le=1_000_000)
    deepspace_session_export_formats: list[str] = Field(default=["jsonl", "json"])

    # Out-of-process subagent providers.
    # Configure ACP, Claude Code, Codex, and NeoSIS SDK backends.
    deepspace_subagent_providers: dict[str, dict] = Field(default_factory=dict)
    deepspace_subagent_default_backend: str = "in-process"

    # Webhook ingress.
    # Signed GitHub webhook adapter that creates sessions from push/PR events.
    deepspace_webhook_github_enabled: bool = False
    deepspace_webhook_github_path: str = "/webhooks/github"
    deepspace_webhook_github_secret_env: str = "GITHUB_WEBHOOK_SECRET"
    deepspace_webhook_github_max_body_bytes: int = Field(default=1_000_000, ge=1024, le=10_000_000)
    deepspace_webhook_rules_enabled: bool = True

    # The Agent Client Protocol bridge.  It lets a trusted programmatic client
    # drive DeepSpace conversations over JSON-RPC.
    deepspace_acp_enabled: bool = False

    # The DeepSpace proactive daemon.  It looks for the tenant-owned
    # recurring schedules that are due and enqueues their runs, for a
    # deployment that does not run the Celery beat scheduler.  It is off by
    # default.
    deepspace_proactive_daemon_enabled: bool = False
    # How many seconds the daemon waits between two passes over the
    # schedules.
    deepspace_proactive_daemon_interval_seconds: int = Field(default=60, ge=1, le=3_600)

    # The directory that holds deployment-installed DeepSpace extensions.
    # An empty value uses ``<local_storage_path>/extensions``.
    deepspace_extensions_dir: str | None = None

    # The number of recent messages the basic compaction stage always keeps.
    # Older complete exchanges are replaced by one marker so a long
    # conversation still fits without dropping the current instruction.
    deepspace_compaction_keep_recent_messages: int = Field(default=24, ge=4, le=200)

    # Interactive DeepSpace transport safety. This bounds an idle provider
    # stream, but never limits the model's output token budget.  Keep the
    # default aligned with the maximum supported model-provider window so
    # deliberate reasoning before the first token is not cut off at 90s.
    deepspace_provider_read_timeout_seconds: int = Field(default=300, ge=15, le=300)
    # Enables only provider-native, ephemeral prompt caching paths that are
    # explicitly implemented by an adapter. It never enables remote durable
    # conversation state or sends provider-specific fields to custom endpoints.
    deepspace_provider_prompt_caching_enabled: bool = True

    minio_endpoint: str = "minio:9000"
    minio_access_key: str = DEFAULT_MINIO_ACCESS_KEY
    minio_secret_key: str = DEFAULT_MINIO_SECRET_KEY
    minio_secure: bool = False
    minio_verify_ssl: bool = True
    minio_bucket: str = "aks-documents"

    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:11030",
            "http://127.0.0.1:11030",
            "http://localhost:10030",
            "http://127.0.0.1:10030",
            "http://localhost:1030",
            "http://127.0.0.1:1030",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    # -----------------------------------------------------------------------
    # DeepSpace session surfaces (the P2 UI/UX parity features)
    # -----------------------------------------------------------------------

    # The goal bar above the composer reads the conversation's goals. The
    # bar is read-only: a goal is created by the agent through the goal
    # tool, so a client cannot invent one.
    deepspace_goal_bar_enabled: bool = True

    # The jobs panel in the side panel lists the conversation's queued
    # turns and artifact jobs.
    deepspace_jobs_panel_enabled: bool = True

    # The composer offers the workspace's skills as an @-style reference.
    deepspace_skill_references_enabled: bool = True

    # The transcript records a workflow run as its own node.
    deepspace_workflow_runs_enabled: bool = True

    # The trajectory ledger pairs each tool call with its result and reports
    # the elapsed time of every step and turn.
    deepspace_trajectory_ledger_enabled: bool = True

    # The deliverables card lists the files a turn declared and the files it
    # changed.
    deepspace_deliverables_card_enabled: bool = True

    # The settings screen lists the deployment's plugins and what each one
    # contributes.
    deepspace_plugin_inventory_enabled: bool = True

    # The subagent catalog lists a conversation's delegated children and the
    # continuation that is still open for each.
    deepspace_subagent_catalog_enabled: bool = True

    # The deployment persona. A deployment states, in its own words, who the
    # agent is for this customer, and that statement is injected as one
    # system section before every turn. It never replaces the product's own
    # policy.
    deepspace_deployment_persona_enabled: bool = False
    deepspace_deployment_persona_title: str = ""
    deepspace_deployment_persona_instructions: str = ""
    deepspace_deployment_persona_max_chars: int = Field(default=4_000, ge=200, le=50_000)

    # Batch dictation in the composer. The browser records the user, the
    # server never receives audio, and the transcript is inserted as text.
    deepspace_dictation_enabled: bool = True
    deepspace_dictation_provider: Literal["browser", "off"] = "browser"
    deepspace_dictation_language: str = "en-US"
    deepspace_dictation_max_seconds: float = Field(default=300.0, ge=15.0, le=3_600.0)

    # The locale layer. English is the only locale the product ships, so the
    # layer is present, typed, and fixed rather than translated.
    deepspace_locale: str = "en"

    # The "open in the desktop app" action. It offers the installed shell
    # for a conversation instead of asking the user to find it.
    deepspace_open_in_app_enabled: bool = True

    # -----------------------------------------------------------------------
    # DeepSpace: Browser Automation (P0 #6)
    # -----------------------------------------------------------------------

    deepspace_browser_automation_enabled: bool = True
    deepspace_browser_automation_playwright_executable: str = "playwright"
    deepspace_browser_automation_browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    deepspace_browser_automation_headless: bool = True
    deepspace_browser_automation_viewport_width: int = 1280
    deepspace_browser_automation_viewport_height: int = 720
    deepspace_browser_automation_default_timeout_ms: int = 30_000
    deepspace_browser_automation_max_timeout_ms: int = 300_000
    deepspace_browser_automation_screenshot_on_error: bool = True
    deepspace_browser_automation_record_video: bool = False
    deepspace_browser_automation_video_dir: str | None = None
    deepspace_browser_automation_downloads_dir: str | None = None
    deepspace_browser_automation_user_agent: str | None = None
    deepspace_browser_automation_locale: str = "en-US"
    deepspace_browser_automation_timezone_id: str = "UTC"
    deepspace_browser_automation_permissions: list[str] = Field(
        default_factory=lambda: ["geolocation", "notifications"]
    )
    deepspace_browser_automation_extra_args: list[str] = Field(default_factory=list)
    deepspace_browser_automation_proxy: dict[str, str] | None = None

    # -----------------------------------------------------------------------
    # DeepSpace: Computer-Use Provider (P0 #7)
    # -----------------------------------------------------------------------

    deepspace_computer_use_enabled: bool = True
    deepspace_computer_use_provider: Literal["cua", "screenshot_vnc", "screenshot_rdp", "local"] = (
        "screenshot_vnc"
    )
    deepspace_computer_use_vnc_host: str = "localhost"
    deepspace_computer_use_vnc_port: int = 5900
    deepspace_computer_use_vnc_password: str | None = None
    deepspace_computer_use_display_width: int = 1920
    deepspace_computer_use_display_height: int = 1080
    deepspace_computer_use_screenshot_interval_ms: int = 500
    deepspace_computer_use_action_timeout_ms: int = 30_000
    deepspace_computer_use_max_screenshot_size_mb: int = 10
    deepspace_computer_use_click_delay_ms: int = 100
    deepspace_computer_use_type_delay_ms: int = 50
    deepspace_computer_use_temp_dir: str | None = None

    # -----------------------------------------------------------------------
    # DeepSpace: Session Telemetry + OTel (P1 #16)
    # -----------------------------------------------------------------------

    deepspace_telemetry_enabled: bool = False
    deepspace_telemetry_otel_endpoint: str | None = None
    deepspace_telemetry_otel_headers: dict[str, str] = Field(default_factory=dict)
    deepspace_telemetry_service_name: str = "deepspace"
    deepspace_telemetry_environment: str = "production"
    deepspace_telemetry_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    deepspace_telemetry_export_console: bool = False
    deepspace_telemetry_attributes: dict[str, str] = Field(default_factory=dict)

    # -----------------------------------------------------------------------
    # DeepSpace: Runtime Invariant Registry (P1 #17)
    # -----------------------------------------------------------------------

    deepspace_invariant_enabled: bool = True
    deepspace_invariant_tool_budget: int = 50
    deepspace_invariant_provider_latency_ms: int = 120_000
    deepspace_invariant_max_turns: int = 1000
    deepspace_invariant_subagent_depth: int = 16
    deepspace_invariant_max_memory_mb: int = 2048
    deepspace_invariant_min_disk_gb: float = 1.0

    # -----------------------------------------------------------------------
    # DeepSpace: Plugin HMR + Config Editor (P1 #18)
    # -----------------------------------------------------------------------

    deepspace_plugin_hmr_enabled: bool = False
    deepspace_plugin_hmr_interval: int = 5
    deepspace_plugin_hmr_debounce_ms: int = 500
    deepspace_plugin_hmr_reload: bool = True
    deepspace_plugin_hmr_validate: bool = True
    deepspace_plugin_hmr_workspace: bool = True
    deepspace_plugin_hmr_deployment: bool = True
    deepspace_plugin_hmr_editor: bool = True

    # -----------------------------------------------------------------------
    # DeepSpace: Slash-Command Registry (P1 #19)
    # -----------------------------------------------------------------------

    deepspace_slash_commands_enabled: bool = True
    deepspace_slash_max_args: int = 20
    deepspace_slash_max_arg_length: int = 1024
    deepspace_slash_completion_limit: int = 50
    deepspace_slash_help_width: int = 80

    # -----------------------------------------------------------------------
    # DeepSpace: Permission Presets + Auto Review (P1 #20)
    # -----------------------------------------------------------------------

    deepspace_permission_presets_enabled: bool = True
    deepspace_auto_review_enabled: bool = False
    deepspace_auto_review_provider: str | None = None
    deepspace_auto_review_model: str | None = None
    deepspace_auto_review_prompt: str = (
        "Review this tool call for safety. The user has granted auto-review permission. "
        "Respond with 'approve' or 'deny' and a brief reason."
    )
    deepspace_auto_review_timeout_ms: int = 20_000

    # -----------------------------------------------------------------------
    # DeepSpace: Bundled Skills (P1 #21)
    # -----------------------------------------------------------------------

    deepspace_bundled_skills_enabled: bool = True
    deepspace_bundled_office_enabled: bool = True
    deepspace_bundled_badge_enabled: bool = True
    deepspace_bundled_auto_install: bool = True
    deepspace_bundled_skill_dir: str = ".agents/skills"

    # -----------------------------------------------------------------------
    # DeepSpace: Session Format Migration Chain (P1 #25)
    # -----------------------------------------------------------------------

    deepspace_format_migration_enabled: bool = True
    deepspace_format_current_version: int = 4
    deepspace_format_auto_migrate: bool = True
    deepspace_format_backup: bool = True

    # -----------------------------------------------------------------------
    # DeepSpace: Conversation Fork (P1 #26)
    # -----------------------------------------------------------------------

    deepspace_fork_enabled: bool = True
    deepspace_fork_max_depth: int = 10
    deepspace_fork_copy_messages: bool = True
    deepspace_fork_copy_events: bool = True
    deepspace_fork_copy_files: bool = True
    deepspace_fork_suffix: str = " (fork)"

    # -----------------------------------------------------------------------
    # DeepSpace: Session Feedback (P1 #27)
    # -----------------------------------------------------------------------

    deepspace_feedback_enabled: bool = True
    deepspace_feedback_require_comment: bool = True
    deepspace_feedback_max_comment: int = 2000
    deepspace_feedback_categories: tuple[str, ...] = (
        "accuracy",
        "helpfulness",
        "clarity",
        "completeness",
        "safety",
        "other",
    )

    # -----------------------------------------------------------------------
    # DeepSpace: Headless Profile (P1 #28)
    # -----------------------------------------------------------------------

    deepspace_headless_enabled: bool = True
    deepspace_headless_default_tenant: str | None = None
    deepspace_headless_default_user: str | None = None
    deepspace_headless_default_model: str | None = None
    deepspace_headless_max_turns: int = 100
    deepspace_headless_turn_timeout_seconds: int = 300
    deepspace_headless_output_format: Literal["json", "text", "jsonl"] = "json"
    deepspace_headless_show_reasoning: bool = False
    deepspace_headless_show_tools: bool = True

    # -----------------------------------------------------------------------
    # Auth / Security
    # -----------------------------------------------------------------------

    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_issuer: str = "ai-knowledge-service"
    jwt_audience: str = "ai-knowledge-service-api"
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 7
    refresh_token_hash_secret: str = DEFAULT_REFRESH_SECRET
    auth_session_retention_days: int = Field(default=90, ge=1, le=3650)

    refresh_cookie_name: str = "aks_refresh_token"
    refresh_cookie_secure: bool = True
    refresh_cookie_samesite: Literal["strict", "lax", "none"] = "strict"
    refresh_cookie_domain: str | None = None
    refresh_cookie_path: str = "/api/v1/auth"
    # Web Push is disabled until all three values are configured in the
    # deployment secret store. The private key is never exposed to clients.
    web_push_vapid_private_key: str | None = None
    web_push_vapid_public_key: str | None = None
    web_push_subject: str | None = None
    bootstrap_super_admin_emails: list[str] = Field(default_factory=list)

    auth_max_failed_attempts: int = 5
    auth_lockout_minutes: int = 15
    admin_break_glass_enabled: bool = False
    provider_secret_backend: Literal["env_keyring", "aws_kms"] = "env_keyring"
    provider_secret_active_kid: str | None = None
    provider_secret_keyring_json: str | None = None
    provider_secret_aws_kms_key_id: str | None = None
    provider_secret_aws_kms_region: str | None = None
    provider_secret_aws_kms_endpoint_url: str | None = None
    provider_secret_audit_reads: bool = True
    totp_secret_active_kid: str | None = None
    totp_secret_keyring_json: str | None = None
    # Sealed collection-chat keyring (signal-pattern-v1 epoch keys). Optional;
    # enabling chat encryption without this pair fails closed with 503.
    collection_chat_active_kid: str | None = None
    collection_chat_keyring_json: str | None = None
    auth_oauth_redirect_uri: str | None = None
    auth_oauth_frontend_redirect_uri: str | None = None
    auth_google_oauth_client_id: str | None = None
    auth_google_oauth_client_secret: str | None = None
    auth_github_oauth_client_id: str | None = None
    auth_github_oauth_client_secret: str | None = None
    provider_openai_oauth_enabled: bool = False
    provider_openai_oauth_official_support_verified: bool = False
    provider_openai_oauth_client_id: str | None = None
    provider_openai_oauth_redirect_uri: str | None = None
    provider_openai_oauth_allowed_redirect_uris: list[str] = Field(default_factory=list)
    provider_openai_oauth_authorize_url: str = "https://auth.openai.com/oauth/authorize"
    provider_openai_oauth_token_url: str = "https://auth.openai.com/oauth/token"
    connector_oauth_redirect_uri: str | None = None
    connector_oauth_frontend_redirect_uri: str | None = None
    connector_google_oauth_client_id: str | None = None
    connector_google_oauth_client_secret: str | None = None
    connector_github_oauth_client_id: str | None = None
    connector_github_oauth_client_secret: str | None = None
    connector_slack_oauth_client_id: str | None = None
    connector_slack_oauth_client_secret: str | None = None
    connector_notion_oauth_client_id: str | None = None
    connector_notion_oauth_client_secret: str | None = None
    # Dedicated native remote MCP OAuth credentials. These are intentionally
    # separate from connector_* settings used by existing integrations.
    mcp_google_oauth_client_id: str | None = None
    mcp_google_oauth_client_secret: str | None = None
    mcp_github_oauth_client_id: str | None = None
    mcp_github_oauth_client_secret: str | None = None
    mcp_slack_oauth_client_id: str | None = None
    mcp_slack_oauth_client_secret: str | None = None
    mcp_oauth_redirect_uri: str | None = None
    composio_api_key: str | None = None
    mcp_catalog_max_age_seconds: int = Field(default=900, ge=60, le=86_400)
    # The complete connected MCP catalogue is always server-side. DeepSpace
    # exposes only the broker surface and just-in-time compact metadata.
    deepspace_mcp_max_search_results: int = Field(default=5, ge=1, le=20)
    deepspace_mcp_max_schema_chars: int = Field(default=6_000, ge=1_000, le=20_000)
    deepspace_mcp_max_result_chars: int = Field(default=12_000, ge=2_000, le=100_000)
    deepspace_mcp_max_calls_per_turn: int = Field(default=8, ge=1, le=32)
    deepspace_mcp_max_discovery_calls_per_turn: int = Field(default=8, ge=1, le=32)
    deepspace_mcp_max_result_chars_per_turn: int = Field(default=60_000, ge=4_000, le=500_000)
    deepspace_mcp_max_argument_chars_per_call: int = Field(default=20_000, ge=1_000, le=100_000)
    averqel_domain: str | None = Field(default=None, validation_alias="AVERQEL_DOMAIN")
    averqel_public_origin: str | None = Field(
        default=None, validation_alias="AVERQEL_PUBLIC_ORIGIN"
    )
    livekit_url: str = Field(default="http://livekit:7880", validation_alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="devkey", validation_alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(
        default="cohesive-voice-secret-key-1092", validation_alias="LIVEKIT_API_SECRET"
    )

    # -----------------------------------------------------------------------
    # Upload / Parsing
    # -----------------------------------------------------------------------

    upload_max_bytes: int = 26_214_400
    tenant_max_storage_bytes: int = 1_073_741_824

    # Upload security. Production deployments must provide a reachable
    # ClamAV daemon; uploads fail closed when the scanner is unavailable.
    malware_scan_enabled: bool = True
    malware_scan_required: bool = False
    malware_scan_host: str = "clamav"
    malware_scan_port: int = 3310
    malware_scan_timeout_seconds: int = 15
    document_event_stream_ticket_ttl_seconds: int = 30

    upload_allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "text/plain",
            "text/markdown",
            "text/x-markdown",
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/tiff",
            "image/webp",
            "image/bmp",
            "image/gif",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/msword",
            "application/vnd.ms-powerpoint",
            "application/vnd.ms-excel",
            "application/vnd.ms-office",
            "application/x-ole-storage",
            "application/CDFV2",
            "application/x-cdf",
            "application/x-composite-document-file",
            "application/zip",
            "application/x-zip-compressed",
            "application/json",
            "application/xml",
            "text/xml",
            "text/csv",
            "text/tab-separated-values",
            "application/x-ipynb+json",
            "application/x-sh",
            "application/javascript",
            "application/typescript",
            "text/x-python",
            "application/octet-stream",
        ]
    )

    upload_allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            ".pdf",
            ".txt",
            ".md",
            ".png",
            ".jpg",
            ".jpeg",
            ".tiff",
            ".tif",
            ".webp",
            ".bmp",
            ".gif",
            ".doc",
            ".docx",
            ".docm",
            ".dot",
            ".dotx",
            ".dotm",
            ".odt",
            ".ott",
            ".odm",
            ".oth",
            ".rtf",
            ".ppt",
            ".pptx",
            ".pptm",
            ".pot",
            ".potx",
            ".potm",
            ".pps",
            ".ppsx",
            ".ppsm",
            ".odp",
            ".otp",
            ".xls",
            ".xlsx",
            ".xlsm",
            ".xlt",
            ".xltx",
            ".xltm",
            ".xlm",
            ".xla",
            ".xlw",
            ".xlsb",
            ".ods",
            ".ots",
            ".odg",
            ".otg",
            ".odc",
            ".otc",
            ".odf",
            ".otf",
            ".odi",
            ".oti",
            # Apple iWork and WPS originals are retained for download.  Their
            # ingestion path provides bounded text when the package exposes it.
            ".pages",
            ".numbers",
            ".key",
            ".wps",
            ".dps",
            ".et",
            ".py",
            ".js",
            ".ts",
            ".java",
            ".go",
            ".rs",
            ".c",
            ".cpp",
            ".cs",
            ".php",
            ".rb",
            ".swift",
            ".kt",
            ".scala",
            ".sql",
            ".yaml",
            ".yml",
            ".json",
            ".xml",
            ".html",
            ".css",
            ".sh",
            ".toml",
            ".ini",
            ".cfg",
            ".log",
            ".csv",
            ".tsv",
            ".ipynb",
        ]
    )

    parser_max_pdf_pages: int = 1000
    parser_max_text_chars: int = 5_000_000
    legacy_conversion_enabled: bool = True
    legacy_conversion_timeout_seconds: int = 60
    extraction_low_coverage_threshold: float = 0.35

    # -----------------------------------------------------------------------
    # OCR / Vision
    # -----------------------------------------------------------------------

    ocr_enabled: bool = True
    ocr_engine: Literal["paddleocr"] = "paddleocr"
    ocr_device: Literal["cpu", "gpu"] = "cpu"
    ocr_enable_mkldnn: bool = False
    ocr_timeout_seconds: int = 20
    ocr_max_pages: int = 200
    ocr_min_confidence: float = 0.45
    ocr_languages: list[str] = Field(default_factory=lambda: ["eng"])
    pdf_image_dpi: int = 200
    ocr_retry_attempts: int = 1
    ocr_max_image_pixels: int = 25_000_000
    ocr_max_image_width: int = 10_000
    ocr_max_image_height: int = 10_000

    vision_enabled: bool = False
    vision_provider: Literal["local", "api"] = "local"
    vision_timeout_seconds: int = 20
    vision_max_pages: int = 200
    vision_retry_attempts: int = 1
    vision_cost_guard_enabled: bool = True
    vision_min_confidence: float = 0.5
    vision_tenant_allowlist: list[str] = Field(default_factory=list)

    # -----------------------------------------------------------------------
    # Ingestion / Chunking
    # -----------------------------------------------------------------------

    ingestion_max_attempts: int = 3
    ingestion_retry_backoff_seconds: int = 2

    chunk_size: int = 800
    chunk_overlap: int = 100
    chunk_min_length: int = 40

    # -----------------------------------------------------------------------
    # Embeddings / LLM
    #
    # Compatibility baseline:
    # These settings are the current env-driven provider contract used by the
    # live query and embedding runtime. Planned provider-management work must
    # remain additive until DB-backed provider selection is fully introduced.
    # -----------------------------------------------------------------------

    ai_integration_scope: Literal["embeddings_only", "embeddings_and_generation"] = (
        "embeddings_only"
    )

    embedding_dimension: int = 384
    embedding_provider: Literal[
        "local-deterministic",
        "sentence-transformers",
        "openai",
        "groq",
        "groq-openai-compatible",
        "mistral",
        "together",
        "fireworks",
        "perplexity",
        "ollama",
        "vllm",
        "lmstudio",
        "custom",
    ] = "local-deterministic"
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_batch_size: int = 32
    embedding_normalize: bool = True
    embedding_timeout_seconds: int = 8
    embedding_retry_attempts: int = 3
    embedding_circuit_breaker_threshold: int = 3
    embedding_circuit_breaker_reset_seconds: int = 30
    local_models_root: str = "/app/models"
    local_inference_base_url: str = "http://inference:1011"
    local_inference_timeout_seconds: int = 60
    local_inference_embedding_concurrency: int = 1
    local_inference_rerank_concurrency: int = 1
    reranking_enabled: bool = True
    reranking_provider: Literal["disabled", "sentence-transformers", "cohere"] = (
        "sentence-transformers"
    )
    reranking_model: str = "BAAI/bge-reranker-v2-m3"
    reranking_timeout_seconds: int = 8
    reranking_top_k_retrieve: int = 12
    reranking_top_k_rerank: int = 8
    reranking_top_k_answer: int = 5
    local_model_warmup_enabled: bool = True

    # -----------------------------------------------------------------------
    # Local Vector Search (sqlite-vec + FTS5)
    # -----------------------------------------------------------------------
    local_vector_enabled: bool = True
    local_vector_dimension: int = 384
    local_vector_db_path: str | None = None  # defaults to ~/.averqel/db/vector.db
    sqlite_vec_extension_path: str | None = None  # auto-detect if None

    llm_provider: Literal[
        "disabled",
        "openai",
        "deepseek",
        "larprouter",
        "groq",
        "groq-openai-compatible",
        "mistral",
        "meta",
        "cline",
        "together",
        "fireworks",
        "perplexity",
        "ollama",
        "vllm",
        "lmstudio",
        "custom",
        "larprouter",
    ] = "disabled"
    llm_model: str = ""
    llm_api_base_url: str = ""
    llm_api_key: str | None = None
    llm_temperature: float = 0.1
    # Legacy fallback for provider paths that require an explicit output
    # budget. DeepSpace does not send this value when model discovery has no
    # advertised output limit; it lets OpenAI-compatible providers choose.
    llm_max_tokens_per_request: int = 1024
    llm_max_requests_per_minute: int = 30
    llm_monthly_budget_usd: float = 50.0
    llm_cost_per_1m_input_tokens_usd: float = 0.075
    llm_cost_per_1m_output_tokens_usd: float = 0.30

    @property
    def max_context_chars(self) -> int:
        """
        Backward-compatible fallback for older runtime code.

        The real context window should come from the selected provider/model.
        When that is unavailable, use a conservative estimate derived from the
        configured request token cap so existing call sites never crash.
        """
        return max(self.llm_max_tokens_per_request * 4, 4_096)

    # -----------------------------------------------------------------------
    # Query / Rate Limits / Retention
    # -----------------------------------------------------------------------

    query_top_k_min: int = 1
    query_top_k_max: int = 25
    query_cache_ttl_seconds: int = 300
    query_no_result_answer_text: str = "No relevant information found for the requested query."
    benchmark_week3_query_script_path: str = "scripts/benchmark_week3_queries.py"

    rate_limit_queries_per_user_per_minute: int = 60
    rate_limit_global_per_ip_per_5_minutes: int = 10000
    rate_limit_upload_per_user_per_5_minutes: int = 20
    rate_limit_auth_login_per_tenant_email_per_5_minutes: int = 30
    rate_limit_support_submissions_per_hour: int = 10
    rate_limit_feedback_submissions_per_hour: int = 20
    rate_limit_public_contact_submissions_per_hour: int = Field(default=5, ge=1, le=100)
    rate_limit_public_contact_verification_ip_per_hour: int = Field(default=10, ge=1, le=100)
    rate_limit_public_contact_verification_email_per_hour: int = Field(default=3, ge=1, le=20)
    rate_limit_auth_refresh_per_ip_per_5_minutes: int = 60
    rate_limit_auth_logout_per_user_per_5_minutes: int = 60
    collection_ws_messages_per_user_per_minute: int = 120
    collection_reports_per_user_per_5_minutes: int = Field(default=10, ge=1, le=100)
    collection_ws_connections_per_user: int = 8
    collection_chat_page_size: int = 100
    # Beta phase: every non-admin registration is granted the editor role
    # free until production. Flip to False when paid subscriptions launch;
    # no code change needed anywhere else.
    beta_free_editor_enabled: bool = True
    beta_notice_resurface_hours: int = 36

    provider_timeout_seconds: int = 8
    provider_retry_attempts: int = 3
    provider_circuit_breaker_threshold: int = 3
    provider_circuit_breaker_reset_seconds: int = 30

    audit_log_retention_days: int = 90
    transient_record_retention_days: int = 30
    # Archive is metadata-only and remains disabled unless an explicitly
    # non-production environment opts in. Permanent purge has no setting.
    storage_retention_automatic_archive_enabled: bool = False

    celery_task_always_eager: bool = True
    notification_smtp_host: str | None = None
    notification_smtp_port: int = Field(default=587, ge=1, le=65535)
    notification_smtp_username: str | None = None
    notification_smtp_password: str | None = Field(default=None, repr=False)
    notification_smtp_from: str | None = None
    notification_smtp_starttls: bool = True
    notification_smtp_implicit_tls: bool = False
    public_contact_recipient: str | None = Field(default=None, repr=False)
    public_contact_from: str | None = None
    public_contact_verification_from: str | None = None
    public_contact_address: str | None = None
    public_contact_resend_api_key: str | None = Field(default=None, repr=False)
    public_contact_resend_webhook_secret: str | None = Field(default=None, repr=False)
    notification_retention_days: int = 365
    notification_delivery_retention_days: int = 90

    # OpenTelemetry is process instrumentation, not a runtime-selection flag.
    otel_enabled: bool = False
    otel_service_name: str = "averqel-api"
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_exporter_otlp_insecure: bool = True

    # -----------------------------------------------------------------------
    # Field Validators
    # -----------------------------------------------------------------------

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ENV_ALLOWED:
            raise ValueError(f"env must be one of {sorted(ENV_ALLOWED)}")
        return normalized

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        normalized = value.strip().upper()
        if normalized not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return normalized

    @field_validator("api_prefix", "refresh_cookie_path")
    @classmethod
    def validate_slash_prefixed_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.startswith("/"):
            raise ValueError("path values must start with '/'")
        return cleaned.rstrip("/") or "/"

    @field_validator("jwt_secret", "refresh_token_hash_secret")
    @classmethod
    def validate_secret_length(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 32:
            raise ValueError("security secrets must be at least 32 characters")
        return cleaned

    @field_validator("database_url", "redis_url")
    @classmethod
    def validate_non_empty_connection_urls(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("connection URLs must not be empty")
        return cleaned

    @field_validator("minio_endpoint", "minio_bucket", "jwt_issuer", "jwt_audience", "app_name")
    @classmethod
    def validate_non_empty_strings(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be empty")
        return cleaned

    @field_validator("refresh_cookie_domain")
    @classmethod
    def validate_refresh_cookie_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        return cleaned or None

    @field_validator("bootstrap_super_admin_emails")
    @classmethod
    def validate_bootstrap_super_admin_emails(cls, value: list[str]) -> list[str]:
        normalized = _normalize_str_list([item.lower() for item in value])
        for email in normalized:
            if "@" not in email:
                raise ValueError("bootstrap_super_admin_emails must contain valid email addresses")
        return normalized

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: list[str]) -> list[str]:
        normalized = _normalize_str_list(value)
        if not normalized:
            raise ValueError("cors_origins must contain at least one origin")
        for origin in normalized:
            if not _is_valid_origin(origin):
                raise ValueError(f"Invalid CORS origin: {origin}")
        return normalized

    @field_validator("upload_allowed_mime_types")
    @classmethod
    def validate_upload_allowed_mime_types(cls, value: list[str]) -> list[str]:
        normalized = _normalize_str_list(value)
        if not normalized:
            raise ValueError("upload_allowed_mime_types must contain at least one MIME type")
        return normalized

    @field_validator("upload_allowed_extensions")
    @classmethod
    def validate_upload_allowed_extensions(cls, value: list[str]) -> list[str]:
        normalized = _normalize_ext_list(value)
        if not normalized:
            raise ValueError("upload_allowed_extensions must contain at least one extension")
        return normalized

    @field_validator("ocr_languages")
    @classmethod
    def validate_ocr_languages(cls, value: list[str]) -> list[str]:
        normalized = _normalize_str_list([item.lower() for item in value])
        if not normalized:
            raise ValueError("ocr_languages must contain at least one language")
        return normalized

    @field_validator("vision_tenant_allowlist")
    @classmethod
    def validate_vision_tenant_allowlist(cls, value: list[str]) -> list[str]:
        return _normalize_str_list(value)

    @field_validator("provider_openai_oauth_allowed_redirect_uris")
    @classmethod
    def validate_provider_openai_oauth_allowed_redirect_uris(cls, value: list[str]) -> list[str]:
        normalized = _normalize_str_list(value)
        for redirect_uri in normalized:
            parsed = urlparse(redirect_uri)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    "provider_openai_oauth_allowed_redirect_uris must contain valid http/https URLs"
                )
        return normalized

    @field_validator(
        "jwt_access_ttl_minutes",
        "jwt_refresh_ttl_days",
        "auth_max_failed_attempts",
        "auth_lockout_minutes",
        "upload_max_bytes",
        "tenant_max_storage_bytes",
        "malware_scan_port",
        "malware_scan_timeout_seconds",
        "document_event_stream_ticket_ttl_seconds",
        "legacy_conversion_timeout_seconds",
        "ocr_timeout_seconds",
        "ocr_max_pages",
        "pdf_image_dpi",
        "ocr_retry_attempts",
        "ocr_max_image_pixels",
        "ocr_max_image_width",
        "ocr_max_image_height",
        "vision_timeout_seconds",
        "vision_max_pages",
        "vision_retry_attempts",
        "ingestion_max_attempts",
        "ingestion_retry_backoff_seconds",
        "chunk_size",
        "chunk_overlap",
        "chunk_min_length",
        "embedding_dimension",
        "embedding_batch_size",
        "embedding_timeout_seconds",
        "embedding_retry_attempts",
        "embedding_circuit_breaker_threshold",
        "embedding_circuit_breaker_reset_seconds",
        "reranking_timeout_seconds",
        "reranking_top_k_retrieve",
        "reranking_top_k_rerank",
        "reranking_top_k_answer",
        "llm_max_tokens_per_request",
        "llm_max_requests_per_minute",
        "query_top_k_min",
        "query_top_k_max",
        "query_cache_ttl_seconds",
        "rate_limit_queries_per_user_per_minute",
        "rate_limit_global_per_ip_per_5_minutes",
        "rate_limit_upload_per_user_per_5_minutes",
        "rate_limit_auth_login_per_tenant_email_per_5_minutes",
        "rate_limit_support_submissions_per_hour",
        "rate_limit_feedback_submissions_per_hour",
        "notification_retention_days",
        "notification_delivery_retention_days",
        "rate_limit_auth_refresh_per_ip_per_5_minutes",
        "rate_limit_auth_logout_per_user_per_5_minutes",
        "collection_ws_messages_per_user_per_minute",
        "collection_reports_per_user_per_5_minutes",
        "collection_ws_connections_per_user",
        "collection_chat_page_size",
        "beta_notice_resurface_hours",
        "provider_timeout_seconds",
        "provider_retry_attempts",
        "provider_circuit_breaker_threshold",
        "provider_circuit_breaker_reset_seconds",
        "audit_log_retention_days",
        "transient_record_retention_days",
    )
    @classmethod
    def validate_positive_numbers(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("numeric settings must be positive")
        return value

    @field_validator(
        "llm_monthly_budget_usd",
        "llm_cost_per_1m_input_tokens_usd",
        "llm_cost_per_1m_output_tokens_usd",
    )
    @classmethod
    def validate_positive_float_numbers(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("numeric settings must be positive")
        return value

    @field_validator("llm_temperature")
    @classmethod
    def validate_temperature(cls, value: float) -> float:
        if not 0.0 <= value <= 2.0:
            raise ValueError("llm_temperature must be between 0.0 and 2.0")
        return value

    @field_validator(
        "ocr_min_confidence",
        "vision_min_confidence",
        "extraction_low_coverage_threshold",
    )
    @classmethod
    def validate_percentage_thresholds(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("thresholds must be between 0.0 and 1.0")
        return value

    @field_validator("query_top_k_max")
    @classmethod
    def validate_top_k_max(cls, value: int, info: ValidationInfo) -> int:
        min_value = int(info.data.get("query_top_k_min", 1))
        if value < min_value:
            raise ValueError("query_top_k_max must be greater than or equal to query_top_k_min")
        return value

    @model_validator(mode="after")
    def validate_reranking_limits(self) -> Settings:
        if self.reranking_top_k_retrieve < self.reranking_top_k_rerank:
            raise ValueError(
                "reranking_top_k_retrieve must be greater than or equal to reranking_top_k_rerank"
            )
        if self.reranking_top_k_rerank < self.reranking_top_k_answer:
            raise ValueError(
                "reranking_top_k_rerank must be greater than or equal to reranking_top_k_answer"
            )
        return self

    @field_validator("llm_api_base_url")
    @classmethod
    def validate_llm_api_base_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        if not _is_http_url(cleaned):
            raise ValueError("llm_api_base_url must be a valid http/https URL")
        return cleaned.rstrip("/")

    @field_validator("connector_oauth_redirect_uri", "connector_oauth_frontend_redirect_uri")
    @classmethod
    def validate_connector_oauth_urls(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            return None
        if not _is_http_url(cleaned):
            raise ValueError("connector OAuth redirect URLs must be valid http/https URLs")
        return cleaned

    @field_validator("mcp_oauth_redirect_uri")
    @classmethod
    def validate_mcp_oauth_redirect_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            return None
        if not _is_http_url(cleaned):
            raise ValueError("mcp_oauth_redirect_uri must be a valid http/https URL")
        return cleaned

    @field_validator(
        "llm_model",
        "llm_api_key",
        "provider_secret_active_kid",
        "provider_secret_keyring_json",
        "deepspace_session_log_active_kid",
        "deepspace_session_log_keyring_json",
        "totp_secret_active_kid",
        "totp_secret_keyring_json",
        "collection_chat_active_kid",
        "collection_chat_keyring_json",
        "provider_openai_oauth_client_id",
        "provider_openai_oauth_redirect_uri",
        "mcp_google_oauth_client_id",
        "mcp_google_oauth_client_secret",
        "mcp_github_oauth_client_id",
        "mcp_github_oauth_client_secret",
        "mcp_slack_oauth_client_id",
        "mcp_slack_oauth_client_secret",
        "mcp_oauth_redirect_uri",
        "public_contact_from",
        "public_contact_verification_from",
        "public_contact_address",
        "averqel_domain",
        "averqel_public_origin",
    )
    @classmethod
    def validate_optional_trimmed_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("jwt_secret", "refresh_token_hash_secret")
    @classmethod
    def validate_min_secret_lengths(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if len(cleaned) < 32:
            raise ValueError(f"{info.field_name} must be at least 32 characters long")
        return cleaned

    # -----------------------------------------------------------------------
    # Model Validators
    # -----------------------------------------------------------------------

    @model_validator(mode="after")
    def resolve_llm_provider_presets(self) -> Settings:
        if self.llm_provider == "disabled":
            return self

        preset = LLM_PROVIDER_PRESETS.get(self.llm_provider)
        if preset is None:
            return self

        default_url, default_model = preset

        if not self.llm_api_base_url and default_url:
            object.__setattr__(self, "llm_api_base_url", default_url)

        if not self.llm_model and default_model:
            object.__setattr__(self, "llm_model", default_model)

        return self

    @model_validator(mode="after")
    def validate_llm_requirements(self) -> Settings:
        if self.llm_provider == "disabled":
            return self

        if self.llm_provider == "custom":
            if not self.llm_api_base_url:
                raise ValueError("llm_api_base_url is required when llm_provider=custom")
            if not self.llm_model:
                raise ValueError("llm_model is required when llm_provider=custom")

        if self.llm_provider in LOCAL_LLM_PROVIDERS:
            if not self.llm_api_base_url:
                raise ValueError(
                    f"llm_api_base_url is required for llm_provider={self.llm_provider}"
                )

        if self.ai_integration_scope == "embeddings_and_generation":
            if not self.llm_model:
                raise ValueError("llm_model is required when generation is enabled")

            if (
                self.env != "test"
                and self.llm_provider in REMOTE_LLM_PROVIDERS
                and not self.llm_api_key
            ):
                raise ValueError(
                    f"llm_api_key is required for llm_provider={self.llm_provider} "
                    "when ai_integration_scope=embeddings_and_generation"
                )

            if (
                self.llm_provider == "custom"
                and not self.llm_api_key
                and not _is_local_like_url(self.llm_api_base_url)
            ):
                raise ValueError(
                    "llm_api_key is required for llm_provider=custom when using a non-local endpoint "
                    "and ai_integration_scope=embeddings_and_generation"
                )

        return self

    @model_validator(mode="after")
    def resolve_connector_oauth_defaults(self) -> Settings:
        public_origin = (self.averqel_public_origin or "").strip().rstrip("/")
        if public_origin:
            if not self.connector_oauth_redirect_uri:
                object.__setattr__(
                    self,
                    "connector_oauth_redirect_uri",
                    f"{public_origin}/api/v1/integrations/connectors/oauth/callback",
                )
            if not self.connector_oauth_frontend_redirect_uri:
                object.__setattr__(
                    self,
                    "connector_oauth_frontend_redirect_uri",
                    f"{public_origin}/dashboard",
                )
        return self

    @model_validator(mode="after")
    def resolve_mcp_oauth_defaults(self) -> Settings:
        public_origin = (self.averqel_public_origin or "").strip().rstrip("/")
        if public_origin and not self.mcp_oauth_redirect_uri:
            object.__setattr__(
                self,
                "mcp_oauth_redirect_uri",
                f"{public_origin}{self.api_prefix}/mcp/oauth/callback",
            )
        for provider in ("google", "github", "slack"):
            client_id = getattr(self, f"mcp_{provider}_oauth_client_id")
            client_secret = getattr(self, f"mcp_{provider}_oauth_client_secret")
            if bool(client_id) != bool(client_secret):
                raise ValueError(
                    f"mcp_{provider}_oauth_client_id and mcp_{provider}_oauth_client_secret must be configured together"
                )
        return self

    @model_validator(mode="after")
    def validate_internal_consistency(self) -> Settings:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")

        if self.chunk_min_length > self.chunk_size:
            raise ValueError("chunk_min_length must be less than or equal to chunk_size")

        if self.refresh_cookie_samesite == "none" and not self.refresh_cookie_secure:
            raise ValueError("refresh_cookie_secure must be true when SameSite=None")

        if self.refresh_cookie_domain and self.refresh_cookie_domain.startswith("."):
            object.__setattr__(
                self, "refresh_cookie_domain", self.refresh_cookie_domain.lstrip(".")
            )

        if (
            self.vision_enabled
            and self.vision_provider == "api"
            and not self.vision_cost_guard_enabled
        ):
            raise ValueError(
                "vision_cost_guard_enabled must remain true when vision_enabled=true "
                "and vision_provider=api"
            )

        if (
            self.vision_enabled
            and self.vision_provider == "api"
            and not self.vision_tenant_allowlist
        ):
            raise ValueError(
                "vision_tenant_allowlist must not be empty when vision_enabled=true and vision_provider=api"
            )

        return self

    @model_validator(mode="after")
    def validate_production_hardening(self) -> Settings:
        if self.env not in PRODUCTION_ENVS:
            return self

        if self.jwt_secret == DEFAULT_JWT_SECRET:
            raise ValueError("Production requires a non-default jwt_secret")

        if self.refresh_token_hash_secret == DEFAULT_REFRESH_SECRET:
            raise ValueError("Production requires a non-default refresh_token_hash_secret")

        if self.database_url == DEFAULT_DATABASE_URL:
            raise ValueError("Production requires a non-default database_url")

        if self.deepspace_session_log_encryption and not self.deepspace_session_log_keyring_json:
            raise ValueError(
                "Production requires deepspace_session_log_keyring_json when session-log encryption is enabled"
            )

        if self.minio_access_key == DEFAULT_MINIO_ACCESS_KEY:
            raise ValueError("Production requires a non-default minio_access_key")

        if self.minio_secret_key == DEFAULT_MINIO_SECRET_KEY:
            raise ValueError("Production requires a non-default minio_secret_key")

        if self.notification_smtp_host and self.notification_smtp_from:
            if self.notification_smtp_starttls == self.notification_smtp_implicit_tls:
                raise ValueError(
                    "Production notification email requires exactly one SMTP TLS mode: "
                    "STARTTLS or implicit TLS"
                )
            if bool(self.notification_smtp_username) != bool(self.notification_smtp_password):
                raise ValueError(
                    "Notification SMTP username and password must be configured together"
                )
            if not (self.averqel_public_origin or self.averqel_domain):
                raise ValueError(
                    "Production notification email requires averqel_public_origin or averqel_domain"
                )

        public_contact_inbound_settings = (
            self.public_contact_address,
            self.public_contact_resend_api_key,
            self.public_contact_resend_webhook_secret,
        )
        if any(public_contact_inbound_settings) and not all(public_contact_inbound_settings):
            raise ValueError(
                "Resend inbound contact forwarding requires the contact address, API key, "
                "and webhook signing secret"
            )

        if not self.refresh_cookie_secure:
            raise ValueError("Production requires refresh_cookie_secure=true")

        if self.minio_secure is False and not _is_private_service_host(self.minio_endpoint):
            raise ValueError(
                "Production requires minio_secure=true unless minio_endpoint is an internal private service"
            )

        if not self.connector_oauth_redirect_uri:
            raise ValueError("Production requires connector_oauth_redirect_uri")

        if not self.connector_oauth_frontend_redirect_uri:
            raise ValueError("Production requires connector_oauth_frontend_redirect_uri")

        if not self.connector_oauth_redirect_uri.startswith("https://"):
            raise ValueError("Production requires connector_oauth_redirect_uri to use https")

        if not self.connector_oauth_frontend_redirect_uri.startswith("https://"):
            raise ValueError(
                "Production requires connector_oauth_frontend_redirect_uri to use https"
            )

        if not self.malware_scan_enabled or not self.malware_scan_required:
            raise ValueError("Production requires malware scanning to be enabled and required")
        if not self.malware_scan_host.strip():
            raise ValueError("Production requires malware_scan_host")

        if self.mcp_oauth_redirect_uri and not self.mcp_oauth_redirect_uri.startswith("https://"):
            raise ValueError("Production requires mcp_oauth_redirect_uri to use https")

        if self.llm_provider in REMOTE_LLM_PROVIDERS and not self.llm_api_key:
            raise ValueError(
                f"Production requires llm_api_key for llm_provider={self.llm_provider}"
            )

        if (
            self.llm_provider == "custom"
            and self.llm_api_base_url
            and not _is_local_like_url(self.llm_api_base_url)
            and not self.llm_api_key
        ):
            raise ValueError("Production requires llm_api_key for non-local custom LLM endpoints")

        if (
            self.provider_secret_backend == "env_keyring"  # nosec B105
        ):  # backend selector, not a secret
            if not self.provider_secret_active_kid:
                raise ValueError("Production requires provider_secret_active_kid")

            if not self.provider_secret_keyring_json:
                raise ValueError("Production requires provider_secret_keyring_json")

            keyring = _parse_provider_secret_keyring(self.provider_secret_keyring_json)
            if self.provider_secret_active_kid not in keyring:
                raise ValueError(
                    "provider_secret_active_kid must exist in provider_secret_keyring_json"
                )
        elif not self.provider_secret_aws_kms_key_id:
            raise ValueError("Production requires provider_secret_aws_kms_key_id")

        return self

    @model_validator(mode="after")
    def validate_provider_openai_oauth_gate(self) -> Settings:
        if not self.provider_openai_oauth_enabled:
            return self

        if not self.provider_openai_oauth_official_support_verified:
            raise ValueError(
                "provider_openai_oauth_enabled requires provider_openai_oauth_official_support_verified=true"
            )

        if not self.provider_openai_oauth_client_id:
            raise ValueError(
                "provider_openai_oauth_client_id is required when provider_openai_oauth_enabled=true"
            )

        if not self.provider_openai_oauth_redirect_uri:
            raise ValueError(
                "provider_openai_oauth_redirect_uri is required when provider_openai_oauth_enabled=true"
            )

        if not self.provider_openai_oauth_allowed_redirect_uris:
            raise ValueError(
                "provider_openai_oauth_allowed_redirect_uris must not be empty when provider_openai_oauth_enabled=true"
            )

        redirect_uri = self.provider_openai_oauth_redirect_uri.rstrip("/")
        allowlisted = {
            item.rstrip("/") for item in self.provider_openai_oauth_allowed_redirect_uris
        }
        if redirect_uri not in allowlisted:
            raise ValueError(
                "provider_openai_oauth_redirect_uri must be included in provider_openai_oauth_allowed_redirect_uris"
            )

        return self

    @model_validator(mode="after")
    def validate_provider_secret_keyring(self) -> Settings:
        if self.provider_secret_backend == "aws_kms":  # Backend selector; nosec B105
            if self.provider_secret_active_kid or self.provider_secret_keyring_json:
                raise ValueError(
                    "provider_secret_active_kid/provider_secret_keyring_json are not used when provider_secret_backend=aws_kms"
                )
            if not self.provider_secret_aws_kms_key_id:
                raise ValueError(
                    "provider_secret_aws_kms_key_id is required when provider_secret_backend=aws_kms"
                )
            return self

        has_active_kid = bool(self.provider_secret_active_kid)
        has_keyring = bool(self.provider_secret_keyring_json)

        if has_active_kid != has_keyring:
            raise ValueError(
                "provider_secret_active_kid and provider_secret_keyring_json must be set together"
            )

        if not has_keyring:
            return self

        keyring = _parse_provider_secret_keyring(self.provider_secret_keyring_json)
        if self.provider_secret_active_kid not in keyring:
            raise ValueError(
                "provider_secret_active_kid must exist in provider_secret_keyring_json"
            )

        return self

    @model_validator(mode="after")
    def validate_deepspace_session_log_keyring(self) -> Settings:
        has_active_kid = bool(self.deepspace_session_log_active_kid)
        has_keyring = bool(self.deepspace_session_log_keyring_json)

        if has_active_kid != has_keyring:
            raise ValueError(
                "deepspace_session_log_active_kid and "
                "deepspace_session_log_keyring_json must be set together"
            )

        if not has_keyring:
            return self

        keyring = _parse_aes_gcm_keyring(
            self.deepspace_session_log_keyring_json,
            setting_name="deepspace_session_log_keyring_json",
            key_label="session log",
        )
        if self.deepspace_session_log_active_kid not in keyring:
            raise ValueError(
                "deepspace_session_log_active_kid must exist in deepspace_session_log_keyring_json"
            )

        return self

    @model_validator(mode="after")
    def validate_collection_chat_keyring(self) -> Settings:
        has_active_kid = bool(self.collection_chat_active_kid)
        has_keyring = bool(self.collection_chat_keyring_json)

        if has_active_kid != has_keyring:
            raise ValueError(
                "collection_chat_active_kid and collection_chat_keyring_json must be set together"
            )

        if not has_keyring:
            return self

        keyring = _parse_provider_secret_keyring(self.collection_chat_keyring_json)
        if self.collection_chat_active_kid not in keyring:
            raise ValueError(
                "collection_chat_active_kid must exist in collection_chat_keyring_json"
            )
        for kid, key_bytes in keyring.items():
            if len(key_bytes) != 32:
                raise ValueError(
                    f"collection chat key for kid={kid!r} must decode to 32 bytes for AES-256-GCM"
                )

        return self

    @model_validator(mode="after")
    def validate_totp_secret_keyring(self) -> Settings:
        has_active_kid = bool(self.totp_secret_active_kid)
        has_keyring = bool(self.totp_secret_keyring_json)

        if has_active_kid != has_keyring:
            raise ValueError(
                "totp_secret_active_kid and totp_secret_keyring_json must be set together"
            )

        if not has_keyring:
            return self

        keyring = _parse_provider_secret_keyring(self.totp_secret_keyring_json)
        if self.totp_secret_active_kid not in keyring:
            raise ValueError("totp_secret_active_kid must exist in totp_secret_keyring_json")

        return self

    @property
    def effective_totp_secret_active_kid(self) -> str | None:
        return self.totp_secret_active_kid or self.provider_secret_active_kid

    @property
    def effective_totp_secret_keyring_json(self) -> str | None:
        return self.totp_secret_keyring_json or self.provider_secret_keyring_json


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

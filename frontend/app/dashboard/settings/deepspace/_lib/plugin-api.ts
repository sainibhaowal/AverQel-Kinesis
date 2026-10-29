import { fetchWithAuth } from "@/lib/api";

/** One tool a plugin contributes to the model-facing catalogue. */
export type PluginTool = {
  name: string;
  description: string;
  mode: "read" | "write" | "interactive" | string;
};

/** One plugin as reported by the deployment inventory. */
export type PluginEntry = {
  id: string;
  name: string;
  version: string;
  description: string;
  source: string;
  tools: PluginTool[];
  enabled_by_default: boolean;
  deployment_enabled: boolean;
  available?: boolean;
  available_in_presets: string[];
  has_behaviour: boolean;
  handler_kind: string;
  native_tool?: string | null;
  has_guidance: boolean;
  settings: string[];
};

/** One preset's session-scoped plugin inventory. */
export type SessionPluginPreset = {
  id: string;
  name: string;
  description: string;
  is_default: boolean;
  plugins: PluginEntry[];
  plugin_count: number;
  enabled_plugin_count: number;
  tools: string[];
  tool_count: number;
  ptc_enabled?: boolean;
  sandbox_mode?: string | null;
  reasoning_effort?: string | null;
  subagent_max_depth?: number | null;
  plan_mode_by_default?: boolean;
  spill_threshold_chars?: number | null;
};

/** One core DeepSpace capability that is always part of the runtime. */
export type CoreRuntimeComponent = {
  id: string;
  name: string;
  description: string;
};

/** Counts for the separate core, extension, and tool inventory planes. */
export type PluginCatalogueCounts = {
  extensions: number;
  enabled_extensions: number;
  tools: number;
  native_tools: number;
  compatibility_tools: number;
  extension_tools: number;
};

/** The plugin inventory published by the Harness runtime. */
export type PluginCatalogue = {
  globalPlugins: PluginEntry[];
  sessionPresets: SessionPluginPreset[];
  coreRuntime: CoreRuntimeComponent[];
  counts: PluginCatalogueCounts;
  defaultPreset: string;
  pluginsEnabled: boolean;
  errors: string[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asBoolean(value: unknown): boolean {
  return value === true;
}

function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function toTool(value: unknown): PluginTool {
  const record = isRecord(value) ? value : {};
  return {
    name: asString(record.name),
    description: asString(record.description),
    mode: asString(record.mode) || "read",
  };
}

function toPlugin(value: unknown): PluginEntry {
  const record = isRecord(value) ? value : {};
  const availableInPresets = Array.isArray(record.available_in_presets)
    ? record.available_in_presets.filter((item): item is string => typeof item === "string")
    : [];
  return {
    id: asString(record.id),
    name: asString(record.name) || asString(record.id),
    version: asString(record.version),
    description: asString(record.description),
    source: asString(record.source),
    tools: Array.isArray(record.tools) ? record.tools.map(toTool) : [],
    enabled_by_default: asBoolean(record.enabled_by_default),
    deployment_enabled: asBoolean(record.deployment_enabled),
    available: typeof record.available === "boolean" ? record.available : undefined,
    available_in_presets: availableInPresets,
    has_behaviour: asBoolean(record.has_behaviour),
    handler_kind: asString(record.handler_kind),
    native_tool: typeof record.native_tool === "string" ? record.native_tool : null,
    has_guidance: asBoolean(record.has_guidance),
    settings: Array.isArray(record.settings)
      ? record.settings.filter((item): item is string => typeof item === "string")
      : [],
  };
}

function toPreset(value: unknown): SessionPluginPreset {
  const record = isRecord(value) ? value : {};
  return {
    id: asString(record.id),
    name: asString(record.name) || asString(record.id),
    description: asString(record.description),
    is_default: asBoolean(record.is_default),
    plugins: Array.isArray(record.plugins) ? record.plugins.map(toPlugin) : [],
    plugin_count: asNumber(record.plugin_count),
    enabled_plugin_count: asNumber(record.enabled_plugin_count),
    tools: Array.isArray(record.tools)
      ? record.tools.filter((item): item is string => typeof item === "string")
      : [],
    tool_count: asNumber(record.tool_count),
    ptc_enabled: typeof record.ptc_enabled === "boolean" ? record.ptc_enabled : undefined,
    sandbox_mode:
      record.sandbox_mode === null
        ? null
        : typeof record.sandbox_mode === "string"
          ? record.sandbox_mode
          : undefined,
    reasoning_effort:
      record.reasoning_effort === null
        ? null
        : typeof record.reasoning_effort === "string"
          ? record.reasoning_effort
          : undefined,
    subagent_max_depth:
      record.subagent_max_depth === null
        ? null
        : typeof record.subagent_max_depth === "number"
          ? record.subagent_max_depth
          : undefined,
    plan_mode_by_default:
      typeof record.plan_mode_by_default === "boolean" ? record.plan_mode_by_default : undefined,
    spill_threshold_chars:
      record.spill_threshold_chars === null
        ? null
        : typeof record.spill_threshold_chars === "number"
          ? record.spill_threshold_chars
          : undefined,
  };
}

function toCoreRuntimeComponent(value: unknown): CoreRuntimeComponent {
  const record = isRecord(value) ? value : {};
  return {
    id: asString(record.id),
    name: asString(record.name) || asString(record.id),
    description: asString(record.description),
  };
}

function toCatalogueCounts(value: unknown): PluginCatalogueCounts {
  const record = isRecord(value) ? value : {};
  return {
    extensions: asNumber(record.extensions),
    enabled_extensions: asNumber(record.enabled_extensions),
    tools: asNumber(record.tools),
    native_tools: asNumber(record.native_tools),
    compatibility_tools: asNumber(record.compatibility_tools),
    extension_tools: asNumber(record.extension_tools),
  };
}

/**
 * Read the deployment's global and preset-scoped plugin inventory.
 *
 * @returns The normalized inventory.
 */
export async function listPluginCatalogue(): Promise<PluginCatalogue> {
  const response = (await fetchWithAuth("/deepspace/chats/plugins")) as Response;
  if (!response.ok) {
    throw new Error(`The plugin catalogue could not be loaded (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  const rawGlobal = Array.isArray(record.global_plugins)
    ? record.global_plugins
    : Array.isArray(record.plugins)
      ? record.plugins
      : [];
  const rawPresets = Array.isArray(record.session_presets) ? record.session_presets : [];
  return {
    globalPlugins: rawGlobal.map(toPlugin),
    sessionPresets: rawPresets.map(toPreset),
    coreRuntime: Array.isArray(record.core_runtime)
      ? record.core_runtime.map(toCoreRuntimeComponent)
      : [],
    counts: toCatalogueCounts(record.counts),
    defaultPreset: asString(record.default_preset) || "standard",
    pluginsEnabled: record.plugins_enabled !== false,
    errors: Array.isArray(record.errors)
      ? record.errors.filter((item): item is string => typeof item === "string")
      : [],
  };
}

/**
 * Filter inventory rows by the text shown in the settings search.
 *
 * @param entries - The plugin rows to search.
 * @param query - The user-entered search text.
 * @returns Matching rows in their original order.
 */
export function filterPluginEntries(entries: PluginEntry[], query: string): PluginEntry[] {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return entries;
  return entries.filter((entry) =>
    [
      entry.id,
      entry.name,
      entry.description,
      entry.source,
      ...entry.tools.flatMap((tool) => [tool.name, tool.description]),
    ].some((value) => value.toLocaleLowerCase().includes(normalized)),
  );
}

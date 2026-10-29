import { fetchWithAuth } from "@/lib/api";

/** One named configuration of how the agent runs. */
export interface AgentMode {
  /** The identifier the server records. */
  id: string;
  /** The label the menu shows. */
  name: string;
  /** The one-line explanation the menu shows. */
  description: string;
  /** Whether the mode may write and run a program that calls the tools. */
  ptc_enabled: boolean;
  /** Whether a new conversation starts in plan mode. */
  plan_mode_by_default: boolean;
  /** Whether the user may change the mode's own settings. */
  editable: boolean;
}

/** The modes a conversation may run in, and the deployment's default. */
export interface AgentModeCatalogue {
  modes: AgentMode[];
  defaultMode: string;
  deploymentDefault?: string;
}

export const DEEPSPACE_DEFAULT_PRESET_CHANGED_EVENT = "deepspace:default-preset-changed";

export interface DefaultPresetChangedDetail {
  preset: string;
  deploymentDefault?: string;
}

/** Broadcast a default preset change to local components and cross-window channels. */
export function broadcastDefaultPresetChanged(preset: string, deploymentDefault?: string): void {
  if (typeof window === "undefined") return;
  const detail: DefaultPresetChangedDetail = { preset, deploymentDefault };
  window.dispatchEvent(new CustomEvent(DEEPSPACE_DEFAULT_PRESET_CHANGED_EVENT, { detail }));
  try {
    if (typeof BroadcastChannel !== "undefined") {
      const channel = new BroadcastChannel("deepspace_sync");
      channel.postMessage({ type: DEEPSPACE_DEFAULT_PRESET_CHANGED_EVENT, ...detail });
      channel.close();
    }
  } catch {
    // BroadcastChannel is optional
  }
}

/** Subscribe to default preset changes across components, tabs, or windows. */
export function subscribeDefaultPresetChanged(
  handler: (detail: DefaultPresetChangedDetail) => void,
): () => void {
  if (typeof window === "undefined") return () => {};
  const windowListener = (event: Event) => {
    const customEvent = event as CustomEvent<DefaultPresetChangedDetail>;
    if (customEvent.detail && typeof customEvent.detail.preset === "string") {
      handler(customEvent.detail);
    }
  };
  window.addEventListener(DEEPSPACE_DEFAULT_PRESET_CHANGED_EVENT, windowListener);

  let channel: BroadcastChannel | null = null;
  try {
    if (typeof BroadcastChannel !== "undefined") {
      channel = new BroadcastChannel("deepspace_sync");
      channel.onmessage = (event) => {
        if (
          event.data &&
          event.data.type === DEEPSPACE_DEFAULT_PRESET_CHANGED_EVENT &&
          typeof event.data.preset === "string"
        ) {
          handler({
            preset: event.data.preset,
            deploymentDefault: event.data.deploymentDefault,
          });
        }
      };
    }
  } catch {
    // BroadcastChannel is optional
  }

  return () => {
    window.removeEventListener(DEEPSPACE_DEFAULT_PRESET_CHANGED_EVENT, windowListener);
    if (channel) {
      channel.close();
    }
  };
}

/** The per-user availability returned by the mode settings route. */
export interface AgentModeEnablement {
  preset_id: string;
  enabled: boolean;
}

/** The mode one conversation runs in. */
export interface ConversationAgentMode {
  /** The mode in force. */
  mode: string;
  /** The mode the conversation recorded, when it recorded one. */
  recorded: string | null;
  /** The mode the deployment starts conversations in. */
  deploymentDefault: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asBoolean(value: unknown): boolean {
  return value === true;
}

function toMode(value: unknown): AgentMode {
  const record = isRecord(value) ? value : {};
  return {
    id: asString(record.id),
    name: asString(record.name),
    description: asString(record.description),
    ptc_enabled: asBoolean(record.ptc_enabled),
    plan_mode_by_default: asBoolean(record.plan_mode_by_default),
    editable: asBoolean(record.editable),
  };
}

/**
 * Read the message the server sent with a failed response.
 *
 * @param response - The failed response whose body carries the message.
 * @returns The server message, or null when the body carries none.
 */
async function readErrorMessage(response: Response): Promise<string | null> {
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload)) {
      const error = payload.error;
      if (isRecord(error) && typeof error.message === "string" && error.message.trim()) {
        return error.message.trim();
      }
      if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail.trim();
    }
  } catch {
    // A gateway can answer with a non-JSON body; the status fallback is used then.
  }
  return null;
}

/**
 * List the agent modes a conversation may run in.
 *
 * @returns The modes and the deployment's default.
 */
export async function listAgentModes(): Promise<AgentModeCatalogue> {
  const [response, enablementResponse, defaultResponse] = await Promise.all([
    fetchWithAuth("/deepspace/chats/presets"),
    fetchWithAuth("/deepspace/chats/presets/enablement").catch(() => null),
    fetchWithAuth("/deepspace/chats/presets/default").catch(() => null),
  ]);
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The agent modes could not be loaded (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  const allModes = Array.isArray(record.presets) ? record.presets.map(toMode) : [];
  let modes = allModes;
  if (enablementResponse?.ok) {
    try {
      modes = filterEnabledAgentModes(allModes, await enablementResponse.json());
    } catch {
      // A malformed optional response must not remove the usable catalogue.
    }
  }
  let defaultMode = asString(record.default) || "standard";
  let deploymentDefault = defaultMode;
  if (defaultResponse?.ok) {
    try {
      const defaultPayload: unknown = await defaultResponse.json();
      if (isRecord(defaultPayload)) {
        if (typeof defaultPayload.preset === "string" && defaultPayload.preset) {
          defaultMode = defaultPayload.preset;
        }
        if (
          typeof defaultPayload.deployment_default === "string" &&
          defaultPayload.deployment_default
        ) {
          deploymentDefault = defaultPayload.deployment_default;
        }
      }
    } catch {
      // Fall back to record.default
    }
  }
  return { modes, defaultMode, deploymentDefault };
}

/**
 * Remove modes hidden by the user while preserving malformed catalogue rows.
 *
 * @param modes - The catalogue rows published by the deployment.
 * @param payload - The enablement response, if one was available.
 * @returns The rows the conversation selector may offer.
 */
export function filterEnabledAgentModes(modes: AgentMode[], payload: unknown): AgentMode[] {
  if (!isRecord(payload) || !Array.isArray(payload.enablement)) return modes;
  const disabled = new Set(
    payload.enablement
      .filter(
        (entry): entry is Record<string, unknown> =>
          isRecord(entry) && entry.enabled === false && typeof entry.preset_id === "string",
      )
      .map((entry) => entry.preset_id as string),
  );
  return modes.filter((mode) => !disabled.has(mode.id));
}

/**
 * Read the agent mode one conversation runs in.
 *
 * @param chatId - The conversation to read.
 * @returns The mode in force and the mode the conversation recorded.
 */
export async function readConversationAgentMode(chatId: string): Promise<ConversationAgentMode> {
  const response = (await fetchWithAuth(
    `/deepspace/chats/${encodeURIComponent(chatId)}/preset`,
  )) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The agent mode could not be read (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  return {
    mode: asString(record.preset) || "standard",
    recorded: typeof record.recorded === "string" ? record.recorded : null,
    deploymentDefault: asString(record.deployment_default) || "standard",
  };
}

/**
 * Record the agent mode one conversation runs in.
 *
 * @param chatId - The conversation to change.
 * @param mode - The mode to record.
 * @returns The mode that is now in force.
 */
export async function writeConversationAgentMode(
  chatId: string,
  mode: string,
): Promise<ConversationAgentMode> {
  const response = (await fetchWithAuth(`/deepspace/chats/${encodeURIComponent(chatId)}/preset`, {
    method: "PUT",
    body: JSON.stringify({ preset: mode }),
  })) as Response;
  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message ?? `The agent mode could not be changed (HTTP ${response.status}).`);
  }
  const payload: unknown = await response.json();
  const record = isRecord(payload) ? payload : {};
  return {
    mode: asString(record.preset) || mode,
    recorded: typeof record.recorded === "string" ? record.recorded : null,
    deploymentDefault: asString(record.deployment_default) || "standard",
  };
}

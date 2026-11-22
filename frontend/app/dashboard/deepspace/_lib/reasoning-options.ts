export type ReasoningEffort = string;

const SAFE_REASONING_EFFORT = /^[a-z][a-z0-9_-]{0,19}$/;

export function isValidReasoningEffort(value: unknown): value is ReasoningEffort {
  return typeof value === "string" && SAFE_REASONING_EFFORT.test(value);
}

export function formatReasoningEffortLabel(effort: ReasoningEffort): string {
  if (!isValidReasoningEffort(effort)) return "Provider option";
  if (effort === "on") return "On";
  if (effort === "off") return "Off";
  return effort
    .replaceAll("-", "_")
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export type ModelReasoningOptions = {
  supportsReasoning?: boolean;
  supportedReasoningEfforts?: string[];
  reasoningCapabilitiesKnown?: boolean;
  reasoningToggleOnly?: boolean;
  reasoningAlwaysOn?: boolean;
  canDisableReasoning?: boolean;
};

/** List only settings that are valid for this model; null always means Auto. */
export function reasoningOptionsForModel(
  model: ModelReasoningOptions | undefined,
): Array<ReasoningEffort | null> {
  const options: Array<ReasoningEffort | null> = [null];
  if (!model?.reasoningCapabilitiesKnown) return options;
  if (model.supportsReasoning === false) return options;
  if (model.reasoningToggleOnly) {
    options.push("on");
  } else {
    options.push(
      ...(model.supportedReasoningEfforts ?? []).filter(
        (effort) => isValidReasoningEffort(effort) && effort !== "off" && effort !== "auto",
      ),
    );
  }
  if (model.canDisableReasoning) options.push("off");
  return options;
}

const MODEL_METADATA_REFRESH_TTL_MS = 15 * 60 * 1000;

/** Refresh selected model metadata when it has not been checked recently. */
export function shouldRefreshModelMetadata(
  model:
    | {
        contextWindow?: number | null;
        reasoningCapabilitiesKnown?: boolean;
        lastSeenAt?: string | null;
      }
    | undefined,
  now = Date.now(),
): boolean {
  if (!model) return true;
  // A synthetic provider default can already have a verified family profile
  // without ever having been checked against the provider's current catalog.
  // Refresh it once when the provider supports model discovery.
  if (!model.lastSeenAt) return true;
  const lastSeenAt = Date.parse(model.lastSeenAt);
  return !Number.isFinite(lastSeenAt) || now - lastSeenAt >= MODEL_METADATA_REFRESH_TTL_MS;
}

/** Keep a selected effort valid when the active model changes. */
export function normalizeReasoningEffortForModel(
  effort: ReasoningEffort | null,
  model: ModelReasoningOptions | undefined,
): ReasoningEffort | null {
  if (!model?.reasoningCapabilitiesKnown) return null;
  if (effort !== null && !isValidReasoningEffort(effort)) return null;
  if (effort === null) return null;
  if (effort === "off") return model.canDisableReasoning ? "off" : null;
  if (model.reasoningToggleOnly && effort === "on") {
    // Map "on" to a valid provider effort value
    // Use the first supported effort, or "medium" as fallback
    const firstEffort = model.supportedReasoningEfforts?.[0];
    return firstEffort && isValidReasoningEffort(firstEffort) ? firstEffort : "medium";
  }
  return model.supportedReasoningEfforts?.some(
    (candidate) => isValidReasoningEffort(candidate) && candidate === effort,
  )
    ? effort
    : null;
}

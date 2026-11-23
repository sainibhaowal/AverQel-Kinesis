import { describe, expect, it } from "vitest";

import {
  normalizeReasoningEffortForModel,
  reasoningOptionsForModel,
} from "../app/dashboard/deepspace/_lib/reasoning-options";

describe("model reasoning options", () => {
  it("uses the provider default as Auto and never invents a default effort", () => {
    const model = {
      reasoningCapabilitiesKnown: true,
      supportedReasoningEfforts: ["low", "high"],
    };

    expect(reasoningOptionsForModel(model)).toEqual([null, "low", "high"]);
    expect(normalizeReasoningEffortForModel(null, model)).toBeNull();
  });

  it("shows Off only when the selected model advertises a disable control", () => {
    const model = {
      reasoningCapabilitiesKnown: true,
      supportedReasoningEfforts: ["low", "high"],
      canDisableReasoning: true,
    };

    expect(reasoningOptionsForModel(model)).toEqual([null, "low", "high", "off"]);
    expect(normalizeReasoningEffortForModel("off", model)).toBe("off");
    expect(
      normalizeReasoningEffortForModel("off", {
        reasoningCapabilitiesKnown: true,
        supportedReasoningEfforts: ["low", "high"],
      }),
    ).toBeNull();
  });

  it("keeps binary and unknown models model-specific", () => {
    expect(
      reasoningOptionsForModel({
        reasoningCapabilitiesKnown: true,
        reasoningToggleOnly: true,
        canDisableReasoning: true,
      }),
    ).toEqual([null, "on", "off"]);
    expect(reasoningOptionsForModel(undefined)).toEqual([null]);
    expect(
      reasoningOptionsForModel({
        reasoningCapabilitiesKnown: true,
        supportsReasoning: false,
        reasoningToggleOnly: true,
        canDisableReasoning: true,
      }),
    ).toEqual([null]);
  });
});

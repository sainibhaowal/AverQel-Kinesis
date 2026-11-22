import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DeepSpaceComposer from "../app/dashboard/deepspace/_components/DeepSpaceComposer";
import {
  formatReasoningEffortLabel,
  isValidReasoningEffort,
  normalizeReasoningEffortForModel,
  reasoningOptionsForModel,
  shouldRefreshModelMetadata,
} from "../app/dashboard/deepspace/_lib/reasoning-options";

function openReasoningMenu(model: {
  supportsReasoning?: boolean;
  supportedReasoningEfforts?: string[];
  reasoningCapabilitiesKnown?: boolean;
  reasoningToggleOnly?: boolean;
  reasoningAlwaysOn?: boolean;
  canDisableReasoning?: boolean;
}) {
  const onReasoningEffortChange = vi.fn();
  render(
    <DeepSpaceComposer
      query=""
      isStreaming={false}
      modelName="custom-model"
      availableModels={[
        {
          providerId: "custom-provider",
          modelName: "custom-model",
          displayName: "Custom model",
          ...model,
        },
      ]}
      onReasoningEffortChange={onReasoningEffortChange}
      onQueryChange={vi.fn()}
      onSubmit={vi.fn()}
      onStop={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Choose reasoning effort" }));
  return onReasoningEffortChange;
}

describe("custom provider reasoning options", () => {
  it("turns reasoning off when a model has no configured reasoning controls", () => {
    expect(
      normalizeReasoningEffortForModel("medium", { reasoningCapabilitiesKnown: true }),
    ).toBeNull();
  });

  it("keeps unknown models on their provider default instead of inventing effort levels", () => {
    expect(
      normalizeReasoningEffortForModel("medium", { reasoningCapabilitiesKnown: false }),
    ).toBeNull();

    openReasoningMenu({ reasoningCapabilitiesKnown: false });

    expect(screen.getByRole("button", { name: "Choose reasoning effort" })).toHaveTextContent(
      "Think Auto",
    );
    expect(screen.getByRole("button", { name: "Provider default" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Low" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Medium" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "High" })).not.toBeInTheDocument();
  });

  it("preserves a supported effort and clears an unsupported one", () => {
    const model = {
      reasoningCapabilitiesKnown: true,
      supportedReasoningEfforts: ["low", "high"],
    };
    expect(normalizeReasoningEffortForModel("high", model)).toBe("high");
    expect(normalizeReasoningEffortForModel("medium", model)).toBeNull();
  });

  it("renders safe model-advertised levels beyond the built-in labels", () => {
    const model = {
      reasoningCapabilitiesKnown: true,
      supportedReasoningEfforts: ["minimal", "ultra", "custom_tier_7"],
    };
    openReasoningMenu(model);

    expect(screen.getByRole("button", { name: "Minimal" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ultra" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Custom Tier 7" })).toBeInTheDocument();
    expect(normalizeReasoningEffortForModel("ultra", model)).toBe("ultra");
    expect(formatReasoningEffortLabel("tier_5")).toBe("Tier 5");
    expect(isValidReasoningEffort("invalid/value")).toBe(false);
  });

  it("uses the binary On option for toggle-only endpoints", () => {
    expect(
      normalizeReasoningEffortForModel("on", {
        reasoningCapabilitiesKnown: true,
        reasoningToggleOnly: true,
      }),
    ).toBe("on");
  });

  it("keeps always-on models on their default without inventing an effort", () => {
    const model = {
      reasoningCapabilitiesKnown: true,
      reasoningAlwaysOn: true,
      supportedReasoningEfforts: [],
    };
    expect(normalizeReasoningEffortForModel(null, model)).toBeNull();

    openReasoningMenu(model);

    expect(screen.getByRole("button", { name: "Choose reasoning effort" })).toHaveTextContent(
      "Think On · Default",
    );
    expect(screen.getByRole("button", { name: "Provider default (always on)" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Off" })).not.toBeInTheDocument();
  });

  it("shows only the provider default when no reasoning control is advertised", () => {
    openReasoningMenu({ reasoningCapabilitiesKnown: true });

    expect(screen.getByRole("button", { name: "Provider default" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Off" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Low" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Medium" })).not.toBeInTheDocument();
  });

  it("shows Off only when a toggle-only model advertises a disable control", () => {
    openReasoningMenu({
      reasoningCapabilitiesKnown: true,
      supportsReasoning: true,
      reasoningToggleOnly: true,
      canDisableReasoning: true,
    });

    expect(screen.getByRole("button", { name: "Off" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "On" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Low" })).not.toBeInTheDocument();
  });

  it("sends the selected model's advertised effort when chosen", () => {
    const onReasoningEffortChange = openReasoningMenu({
      reasoningCapabilitiesKnown: true,
      supportsReasoning: true,
      supportedReasoningEfforts: ["low", "high"],
    });

    fireEvent.click(screen.getByRole("button", { name: "High" }));

    expect(onReasoningEffortChange).toHaveBeenCalledWith("high");
    expect(reasoningOptionsForModel({
      reasoningCapabilitiesKnown: true,
      supportedReasoningEfforts: ["low", "high"],
    })).toEqual([null, "low", "high"]);
  });

  it("shows only the configured effort levels", () => {
    openReasoningMenu({
      reasoningCapabilitiesKnown: true,
      supportedReasoningEfforts: ["low", "high", "extreme_high"],
    });

    expect(screen.getByRole("button", { name: "Low" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "High" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Extreme High" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Medium" })).not.toBeInTheDocument();
  });

  it("refreshes unknown capabilities when model metadata is stale", () => {
    const now = Date.parse("2026-10-10T12:00:00.000Z");
    expect(
      shouldRefreshModelMetadata(
        {
          contextWindow: 128_000,
          reasoningCapabilitiesKnown: false,
          lastSeenAt: "2026-10-10T11:00:00.000Z",
        },
        now,
      ),
    ).toBe(true);
  });

  it("refreshes stale metadata and reuses recently checked descriptors", () => {
    const now = Date.parse("2026-10-10T12:00:00.000Z");
    expect(
      shouldRefreshModelMetadata(
        {
          contextWindow: 128_000,
          reasoningCapabilitiesKnown: true,
          lastSeenAt: null,
        },
        now,
      ),
    ).toBe(true);
    expect(
      shouldRefreshModelMetadata(
        {
          contextWindow: 128_000,
          reasoningCapabilitiesKnown: true,
          lastSeenAt: "2026-10-10T11:55:00.000Z",
        },
        now,
      ),
    ).toBe(false);
    expect(
      shouldRefreshModelMetadata(
        {
          contextWindow: 128_000,
          reasoningCapabilitiesKnown: false,
          lastSeenAt: "2026-10-10T11:55:00.000Z",
        },
        now,
      ),
    ).toBe(false);
    expect(
      shouldRefreshModelMetadata(
        {
          contextWindow: 128_000,
          reasoningCapabilitiesKnown: true,
          lastSeenAt: "2026-10-10T10:00:00.000Z",
        },
        now,
      ),
    ).toBe(true);
  });
});

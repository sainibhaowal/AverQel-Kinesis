import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DeepSpaceSettingsPage from "../app/dashboard/settings/deepspace/page";

const fetchWithAuth = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  fetchWithAuth,
}));

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
}

const standardPreset = {
  id: "standard",
  name: "Standard",
  description: "Every tool allowed by the policy.",
  ptc_enabled: false,
  plan_mode_by_default: false,
  editable: false,
  tools: [],
  sandbox_mode: null,
  reasoning_effort: null,
  subagent_max_depth: null,
  spill_threshold_chars: null,
};

describe("DeepSpace settings visual states", () => {
  beforeEach(() => {
    fetchWithAuth.mockImplementation(async (endpoint: string) => {
      if (endpoint === "/deepspace/sandbox/runtime") {
        return jsonResponse({
          sandbox: { selected: "workspace-write", configured: "workspace-write", available: true },
          shell_enabled: true,
        });
      }
      if (endpoint === "/deepspace/chats/presets") {
        return jsonResponse({ presets: [standardPreset], default: "standard" });
      }
      if (endpoint === "/deepspace/chats/presets/enablement") {
        return jsonResponse({ enablement: [{ preset_id: "standard", enabled: true }] });
      }
      if (endpoint === "/deepspace/chats/presets/default") {
        return jsonResponse({
          preset: "standard",
          recorded: null,
          settings: {},
          deployment_default: "standard",
        });
      }
      throw new Error(`Unhandled endpoint: ${endpoint}`);
    });
  });

  it("keeps mode labels in the row and exposes compact accessible toggles", async () => {
    render(<DeepSpaceSettingsPage />);

    fireEvent.click(screen.getByRole("tab", { name: /agent modes/i }));

    expect(await screen.findByRole("heading", { name: "Available modes" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Standard" })).toBeInTheDocument();
    expect(screen.getByText("Deployment default")).toBeInTheDocument();
    expect(screen.getByText("PTC off")).toBeInTheDocument();
    expect(screen.getByText("Plan off")).toBeInTheDocument();
    expect(screen.getAllByText("Standard")).toHaveLength(2);
  });
});

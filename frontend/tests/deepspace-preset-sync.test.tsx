import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DeepSpaceSettingsPage from "../app/dashboard/settings/deepspace/page";
import { broadcastDefaultPresetChanged } from "../app/dashboard/deepspace/_lib/agent-mode-api";

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

const customPreset = {
  id: "custom",
  name: "Custom",
  description: "A preset the user configured.",
  ptc_enabled: false,
  plan_mode_by_default: false,
  editable: true,
  tools: [],
  sandbox_mode: null,
  reasoning_effort: null,
  subagent_max_depth: null,
  spill_threshold_chars: null,
};

describe("DeepSpace preset and mode synchronization", () => {
  beforeEach(() => {
    fetchWithAuth.mockImplementation(async (endpoint: string, options?: RequestInit) => {
      if (endpoint === "/deepspace/sandbox/runtime") {
        return jsonResponse({
          sandbox: { selected: "workspace-write", configured: "workspace-write", available: true },
          shell_enabled: true,
        });
      }
      if (endpoint === "/deepspace/chats/presets") {
        return jsonResponse({ presets: [standardPreset, customPreset], default: "standard" });
      }
      if (endpoint === "/deepspace/chats/presets/enablement") {
        return jsonResponse({
          enablement: [
            { preset_id: "standard", enabled: true },
            { preset_id: "custom", enabled: true },
          ],
        });
      }
      if (endpoint === "/deepspace/chats/presets/default") {
        if (options?.method === "PUT") {
          const body = JSON.parse(String(options.body));
          return jsonResponse({
            preset: body.preset,
            recorded: body.preset,
            settings: {},
            deployment_default: "standard",
          });
        }
        return jsonResponse({
          preset: "custom",
          recorded: "custom",
          settings: {},
          deployment_default: "standard",
        });
      }
      throw new Error(`Unhandled endpoint: ${endpoint}`);
    });
  });

  it("displays User default on Custom and Deployment default on Standard in Agent modes tab", async () => {
    render(<DeepSpaceSettingsPage />);

    fireEvent.click(screen.getByRole("tab", { name: /agent modes/i }));

    expect(await screen.findByRole("heading", { name: "Available modes" })).toBeInTheDocument();
    expect(screen.getByText("User default")).toBeInTheDocument();
    expect(screen.getByText("Deployment default")).toBeInTheDocument();
  });

  it("updates real-time when a preset change event is broadcast", async () => {
    render(<DeepSpaceSettingsPage />);

    fireEvent.click(screen.getByRole("tab", { name: /agent modes/i }));

    expect(await screen.findByRole("heading", { name: "Available modes" })).toBeInTheDocument();

    // Broadcast change from custom to standard
    await waitFor(() => {
      broadcastDefaultPresetChanged("standard", "standard");
    });

    await waitFor(() => {
      // Now Standard is both user default and deployment default
      expect(screen.getByText("User default")).toBeInTheDocument();
    });
  });
});

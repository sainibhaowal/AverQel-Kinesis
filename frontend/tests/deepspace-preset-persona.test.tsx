import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  settings: {},
};

describe("DeepSpace preset persona editor", () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
    fetchWithAuth.mockImplementation(async (endpoint: string, _options?: RequestInit) => {
      if (endpoint === "/deepspace/sandbox/runtime") {
        return jsonResponse({
          sandbox: { selected: "workspace-write", configured: "workspace-write", available: true },
          shell_enabled: true,
        });
      }
      if (endpoint === "/deepspace/chats/presets") {
        return jsonResponse({ presets: [customPreset], default: "custom" });
      }
      if (endpoint === "/deepspace/chats/presets/enablement") {
        return jsonResponse({ enablement: [{ preset_id: "custom", enabled: true }] });
      }
      if (endpoint === "/deepspace/chats/presets/default") {
        return jsonResponse({
          preset: "custom",
          recorded: "custom",
          settings: {},
          deployment_default: "custom",
        });
      }
      if (endpoint === "/deepspace/chats/plugins") {
        return jsonResponse({
          global_plugins: [],
          session_presets: [],
          core_runtime: [],
          counts: {},
          default_preset: "custom",
          plugins_enabled: true,
          errors: [],
        });
      }
      if (endpoint.startsWith("/deepspace/chats/presets/custom/")) {
        return jsonResponse({});
      }
      throw new Error(`Unhandled endpoint: ${endpoint}`);
    });
  });

  it("saves persona prefix, suffix, and the runtime-context choice", async () => {
    render(<DeepSpaceSettingsPage />);

    fireEvent.click(screen.getByRole("tab", { name: /presets/i }));
    fireEvent.click(await screen.findByRole("button", { name: /customize/i }));

    const prefix = await screen.findByLabelText(/persona prefix/i);
    fireEvent.change(prefix, { target: { value: "You are the team's agent." } });
    const suffix = screen.getByLabelText(/persona suffix/i);
    fireEvent.change(suffix, { target: { value: "Close with the next step." } });

    const context = screen.getByRole("switch", { name: /include runtime context/i });
    expect(context).toHaveAttribute("aria-checked", "true");
    fireEvent.click(context);

    fireEvent.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() => {
      const update = fetchWithAuth.mock.calls.find(
        ([endpoint, options]) =>
          endpoint === "/deepspace/chats/presets/default" &&
          (options as RequestInit | undefined)?.method === "PUT",
      );
      expect(update).toBeDefined();
      const body = JSON.parse(String((update?.[1] as RequestInit | undefined)?.body));
      expect(body.settings.persona_prefix).toBe("You are the team's agent.");
      expect(body.settings.persona_suffix).toBe("Close with the next step.");
      expect(body.settings.persona_include_runtime_context).toBe(false);
    });
  });
});

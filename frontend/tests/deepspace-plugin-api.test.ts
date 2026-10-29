import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchWithAuth } from "@/lib/api";
import {
  filterPluginEntries,
  listPluginCatalogue,
  type PluginEntry,
} from "../app/dashboard/settings/deepspace/_lib/plugin-api";

vi.mock("@/lib/api", () => ({
  fetchWithAuth: vi.fn(),
}));

const mockedFetchWithAuth = vi.mocked(fetchWithAuth);

describe("DeepSpace plugin catalogue", () => {
  beforeEach(() => {
    mockedFetchWithAuth.mockReset();
  });

  it("normalizes the global and preset-scoped inventory", async () => {
    mockedFetchWithAuth.mockResolvedValue(
      new Response(
        JSON.stringify({
          global_plugins: [
            {
              id: "code-metrics",
              name: "Code metrics",
              tools: [{ name: "code_metrics", mode: "read" }],
              deployment_enabled: true,
              available_in_presets: ["standard"],
            },
          ],
          session_presets: [
            {
              id: "standard",
              name: "Standard",
              is_default: true,
              plugin_count: 1,
              enabled_plugin_count: 1,
              tools: ["code_metrics"],
              tool_count: 1,
              plugins: [{ id: "code-metrics", name: "Code metrics" }],
            },
          ],
          core_runtime: [
            {
              id: "agent-loop",
              name: "Agent loop",
              description: "Runs model rounds.",
            },
          ],
          counts: {
            extensions: 1,
            enabled_extensions: 1,
            tools: 1,
            native_tools: 1,
            compatibility_tools: 0,
            extension_tools: 0,
          },
          default_preset: "standard",
          plugins_enabled: true,
          errors: [],
        }),
      ),
    );

    await expect(listPluginCatalogue()).resolves.toMatchObject({
      defaultPreset: "standard",
      pluginsEnabled: true,
      globalPlugins: [expect.objectContaining({ id: "code-metrics", deployment_enabled: true })],
      sessionPresets: [expect.objectContaining({ id: "standard", is_default: true })],
      coreRuntime: [expect.objectContaining({ id: "agent-loop" })],
      counts: expect.objectContaining({ extensions: 1, tools: 1 }),
    });
    expect(mockedFetchWithAuth).toHaveBeenCalledWith("/deepspace/chats/plugins");
  });

  it("searches plugin identity, descriptions, and tool metadata", () => {
    const entries: PluginEntry[] = [
      {
        id: "code-metrics",
        name: "Code metrics",
        version: "1.0.0",
        description: "Counts source files.",
        source: "builtin",
        tools: [{ name: "code_metrics", description: "Counts lines.", mode: "read" }],
        enabled_by_default: false,
        deployment_enabled: true,
        available_in_presets: ["standard"],
        has_behaviour: true,
        handler_kind: "python",
        has_guidance: false,
        settings: [],
      },
      {
        id: "other",
        name: "Other",
        version: "1.0.0",
        description: "A different capability.",
        source: "package",
        tools: [],
        enabled_by_default: false,
        deployment_enabled: true,
        available_in_presets: [],
        has_behaviour: false,
        handler_kind: "python",
        has_guidance: false,
        settings: [],
      },
    ];

    expect(filterPluginEntries(entries, "lines").map((entry) => entry.id)).toEqual([
      "code-metrics",
    ]);
    expect(filterPluginEntries(entries, "")).toHaveLength(2);
  });
});

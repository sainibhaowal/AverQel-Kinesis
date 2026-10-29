import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PluginInventoryTab from "../app/dashboard/settings/deepspace/_components/PluginInventoryTab";

const fetchWithAuth = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  fetchWithAuth,
}));

describe("DeepSpace capability inventory", () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
    fetchWithAuth.mockResolvedValue(
      new Response(
        JSON.stringify({
          global_plugins: [
            {
              id: "native.fs_read",
              name: "Read files",
              description: "Reads workspace files.",
              tools: [{ name: "fs_read", description: "Reads files.", mode: "read" }],
              deployment_enabled: true,
              available_in_presets: ["standard"],
            },
            {
              id: "native.fs_write",
              name: "Write files",
              description: "Writes workspace files.",
              tools: [{ name: "fs_write", description: "Writes files.", mode: "write" }],
              deployment_enabled: true,
              available_in_presets: ["ptc"],
            },
            {
              id: "native.shell",
              name: "Run shell commands",
              description: "Runs shell commands.",
              tools: [{ name: "shell_exec", description: "Runs commands.", mode: "write" }],
              deployment_enabled: false,
              available: false,
              available_in_presets: ["standard"],
            },
          ],
          session_presets: [
            {
              id: "standard",
              name: "Standard",
              description: "Every tool allowed by policy.",
              is_default: true,
              plugin_count: 1,
              enabled_plugin_count: 1,
              tools: ["fs_read"],
              tool_count: 1,
              ptc_enabled: false,
              sandbox_mode: "workspace-write",
              subagent_max_depth: 3,
              plugins: [
                {
                  id: "native.fs_read",
                  name: "Read files",
                  deployment_enabled: true,
                  available: true,
                  tools: [{ name: "fs_read", mode: "read" }],
                },
              ],
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
            extensions: 3,
            enabled_extensions: 2,
            tools: 3,
            native_tools: 3,
            compatibility_tools: 0,
            extension_tools: 0,
          },
          default_preset: "standard",
          plugins_enabled: true,
          errors: [],
        }),
      ),
    );
  });

  it("separates core runtime from optional extensions and tools", async () => {
    render(<PluginInventoryTab />);

    expect(await screen.findByText("DeepSpace capabilities")).toBeInTheDocument();
    expect(screen.getByText("Core DeepSpace runtime")).toBeInTheDocument();
    expect(screen.getByText("Available extensions")).toBeInTheDocument();
    expect(screen.getByText("Preset extensions")).toBeInTheDocument();
    expect(screen.getByText("Agent loop")).toBeInTheDocument();
    expect(screen.getByText("Registered tools")).toBeInTheDocument();
    expect(screen.queryByText("Global plugins")).not.toBeInTheDocument();
    expect(screen.queryByText("Session plugins")).not.toBeInTheDocument();
  });

  it("shows preset runtime state and tool availability while the list is expanded", async () => {
    render(<PluginInventoryTab />);

    expect(await screen.findByText("Standard runtime status")).toBeInTheDocument();
    expect(screen.getByText("PTC off")).toBeInTheDocument();
    expect(screen.getByText("Available: 1")).toBeInTheDocument();
    expect(screen.getByText("Not in preset: 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Off: 1" })).toBeInTheDocument();
    expect(screen.getByText("Not in this preset")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Write files/ }));
    expect(screen.getByText("Off", { exact: true })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Not in preset: 1" }));
    expect(screen.queryByText("Read files")).not.toBeInTheDocument();
    expect(screen.getByText("Write files")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Off: 1" }));
    expect(screen.queryByText("Write files")).not.toBeInTheDocument();
    expect(screen.getByText("Run shell commands")).toBeInTheDocument();
  });
});

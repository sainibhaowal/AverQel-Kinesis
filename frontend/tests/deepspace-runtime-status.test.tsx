import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DeepSpaceSettingsPage from "../app/dashboard/settings/deepspace/page";

const fetchWithAuth = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({ fetchWithAuth }));

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
}

const observedAt = "2026-11-07T12:00:00Z";

describe("DeepSpace runtime status", () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
  });

  it("shows legacy omissions as unknown and keeps the legacy permission policy singular", async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({
        sandbox: {
          selected: "bwrap",
          configured: "auto",
          available: true,
          network: true,
          platform: "linux",
          enforcement: "full",
        },
        permission_mode: "workspace-write",
        shell_enabled: true,
      }),
    );

    render(<DeepSpaceSettingsPage />);

    expect(await screen.findByText("Active · full boundary")).toBeInTheDocument();
    expect(screen.getByText("Read-only / Workspace cage").parentElement).toHaveTextContent(
      "Available",
    );
    expect(screen.getByText("Network isolation support").parentElement).toHaveTextContent(
      "Available",
    );
    expect(screen.getByText("Deployment permission policy (legacy)")).toBeInTheDocument();
    expect(screen.queryByText("Not guaranteed")).not.toBeInTheDocument();
    expect(
      screen.getByText(/backend returned an older or incomplete runtime status/i),
    ).toBeInTheDocument();
  });

  it("distinguishes partial isolation and refreshes status on demand", async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        jsonResponse({
          contract_version: 4,
          observed_at: observedAt,
          sandbox: {
            selected: "landlock",
            configured: "auto",
            available: true,
            network: true,
            network_enforced: false,
            network_isolation_supported: false,
            full_filesystem_isolation: false,
            platform: "linux",
            enforcement: "partial",
            boundary: "partial",
          },
          permission_default: "workspace-write",
          permission_maximum: "danger-full-access",
          restricted_modes_fail_closed: true,
          shell_enabled: true,
          terminal_enabled: false,
          browser_automation: {
            enabled: true,
            available: false,
            browser_type: "chromium",
            reason: "The Playwright Python runtime is not installed.",
          },
          computer_use: {
            enabled: true,
            available: false,
            provider: "local",
            reason: "The local desktop driver needs xdotool.",
          },
          activity: {
            status: "available",
            execution_gate: { status: "available" },
            workers: {
              mode: "embedded-background-pool",
              status: "available",
              worker_count: 4,
            },
            jobs: { queued: 1, running: 2, failed_last_24h: 0 },
            agents: [{ label: "Scout", status: "running", depth: 1 }],
            active_tools: [{ tool_name: "fs_glob", agent: "Scout" }],
          },
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          contract_version: 4,
          observed_at: observedAt,
          sandbox: {
            selected: "bwrap",
            configured: "auto",
            available: true,
            network: true,
            network_enforced: false,
            network_isolation_supported: true,
            full_filesystem_isolation: true,
            platform: "linux",
            enforcement: "full",
            boundary: "full",
          },
          permission_default: "workspace-write",
          permission_maximum: "danger-full-access",
          restricted_modes_fail_closed: true,
          shell_enabled: true,
        }),
      );

    render(<DeepSpaceSettingsPage />);

    expect(await screen.findByText("Available · partial boundary")).toBeInTheDocument();
    expect(screen.getByText("Live agent activity")).toBeInTheDocument();
    expect(
      screen.getByText("available · 4 worker(s) · embedded-background-pool"),
    ).toBeInTheDocument();
    expect(screen.getByText("Shared tool gate").parentElement).toHaveTextContent("available");
    expect(screen.getByLabelText("Active agents")).toHaveTextContent("Scout · running");
    expect(screen.getByLabelText("Active tools")).toHaveTextContent("fs_glob · Scout");
    expect(screen.getByText(/partial operating-system boundary/i)).toBeInTheDocument();
    expect(screen.getByText("Deployment default").parentElement).toHaveTextContent(
      "workspace-write",
    );
    expect(screen.getByText("Deployment maximum").parentElement).toHaveTextContent(
      "danger-full-access",
    );
    expect(screen.getByText("Persistent terminal sessions").parentElement).toHaveTextContent(
      "Disabled",
    );
    expect(screen.getByText("Browser automation").parentElement).toHaveTextContent(
      "Unavailable · The Playwright Python runtime is not installed.",
    );
    expect(screen.getByText("Computer use").parentElement).toHaveTextContent(
      "Unavailable · The local desktop driver needs xdotool.",
    );

    fireEvent.click(screen.getByRole("button", { name: "Refresh DeepSpace runtime status" }));

    await waitFor(() => {
      expect(screen.getByText("Active · full boundary")).toBeInTheDocument();
      expect(fetchWithAuth).toHaveBeenCalledTimes(2);
    });
    expect(screen.getByText("Network in Workspace mode").parentElement).toHaveTextContent(
      "Allowed by deployment policy",
    );
    expect(screen.getByText("Network isolation support").parentElement).toHaveTextContent(
      "Available",
    );
  });
});

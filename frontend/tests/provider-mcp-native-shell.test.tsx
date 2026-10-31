import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ProvidersSettingsPage from "../app/dashboard/settings/providers/page";
import MCPInventoryList from "../app/dashboard/settings/providers/_components/MCPInventoryList";

vi.mock("@/lib/providers-api", () => ({
  listProviders: vi.fn(async () => []),
  listAssignments: vi.fn(async () => []),
  listSupportedProviderTypes: vi.fn(async () => []),
  getOpenAIOAuthStatus: vi.fn(async () => ({
    available: false,
    connected: false,
    provider_type: "openai",
    message: "API key only",
  })),
  listProviderModels: vi.fn(async () => []),
  createProvider: vi.fn(),
  updateProvider: vi.fn(),
  testProvider: vi.fn(),
  refreshProviderModels: vi.fn(async () => []),
  pullProviderModel: vi.fn(),
  createAssignment: vi.fn(),
  updateAssignment: vi.fn(),
  deleteProvider: vi.fn(),
  disconnectProvider: vi.fn(),
  startOpenAIOAuth: vi.fn(),
  rotateProviderSecret: vi.fn(),
}));

vi.mock("@/lib/mcp-api", () => ({
  getComposioSettings: vi.fn(async () => ({
    configured: true,
    masked_key: "comp_••••••••1234",
  })),
  saveComposioSettings: vi.fn(async () => ({
    configured: true,
    masked_key: "comp_••••••••5678",
  })),
  deleteComposioSettings: vi.fn(async () => {}),
  listMCPServers: vi.fn(async () => [
    {
      id: "google-drive-1",
      name: "Google Drive",
      transport: "streamable_http",
      enabled: true,
      status: "connected",
      is_local: false,
      config: {
        provider_slug: "google-drive",
        mcp_catalog_tool_count: 3,
        server_url: "https://mcp.composio.dev/google-drive",
      },
      account_identity: {
        email: "user@example.com",
        display_name: "Google Drive Account",
      },
      catalog_revision: 1,
    },
    {
      id: "local-git-1",
      name: "Local Git Repositories",
      transport: "stdio",
      enabled: true,
      status: "connected",
      is_local: true,
      config: {
        provider_slug: "git",
        mcp_catalog_tool_count: 28,
        command: "npx -y @modelcontextprotocol/server-git",
      },
      account_identity: {
        display_name: "Local System",
      },
      catalog_revision: 1,
    },
  ]),
  toggleMCPServer: vi.fn(async (id: string) => ({
    id,
    name: "Toggled Server",
    enabled: false,
  })),
  deleteMCPServer: vi.fn(async () => {}),
  createCustomMCPServer: vi.fn(async (payload) => ({
    id: "custom-1",
    name: payload.name,
    transport: payload.transport,
    enabled: true,
    status: "connected",
    is_local: payload.transport === "stdio",
    config: {},
    account_identity: {},
    catalog_revision: 1,
  })),
}));

vi.mock("react-hot-toast", () => ({
  default: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

describe("MCP native provider shell layout", () => {
  it("renders the native 2-column shell with MCP inventory and System Navigator empty state", async () => {
    render(<ProvidersSettingsPage />);

    // Switch to MCP Server tab
    const mcpTab = await screen.findByRole("button", { name: /mcp server/i });
    fireEvent.click(mcpTab);

    // Verify left sidebar navigator has MCP Server Families header
    expect(await screen.findByText(/mcp server families/i)).toBeInTheDocument();
    expect(screen.getByText(/1 option/i)).toBeInTheDocument();

    // Verify only the provider family is in the inventory sidebar (no marketplace apps like Google Drive)
    expect(screen.getByText("Composio Provider")).toBeInTheDocument();
    expect(screen.queryByText("Google Drive")).not.toBeInTheDocument();
    expect(screen.queryByText("Local Git Repositories")).not.toBeInTheDocument();

    // Verify right side initially displays the native System Navigator empty state
    expect(screen.getByText("System Navigator")).toBeInTheDocument();
    expect(
      screen.getByText(/select a provider from the inventory to adjust its parameters or test connectivity/i),
    ).toBeInTheDocument();
  });

  it("selects Composio and renders Composio provider setup without marketplace app logos", async () => {
    render(<ProvidersSettingsPage />);

    // Switch to MCP Server tab
    fireEvent.click(await screen.findByRole("button", { name: /mcp server/i }));

    // Click Composio in the left inventory
    const composioBtn = await screen.findByText("Composio Provider");
    fireEvent.click(composioBtn);

    // Verify Composio provider setup panel renders in right panel
    expect(await screen.findByText("Composio API Key Configuration")).toBeInTheDocument();
    expect(screen.getByText(/comp_••••••••1234/)).toBeInTheDocument();
    expect(screen.getByText("Provider Architecture & Connectivity")).toBeInTheDocument();
    expect(screen.getByText("Connect & Use Individual MCP Server Apps")).toBeInTheDocument();
    expect(screen.getByText("Open MCP Servers Page")).toBeInTheDocument();

    // Verify NO individual app badges/logos in the provider setup panel
    expect(screen.queryByText(/supported 1-click apps/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Change Key" }));
    const apiKeyInput = screen.getByLabelText("Composio API Key");
    expect(apiKeyInput).toHaveClass(
      "provider-api-key-input",
      "border-slate-400",
      "dark:border-slate-500",
      "bg-white",
      "dark:bg-[#131b15]",
    );
  });

  it("uses readable light and dark colors for the setup-required badge", async () => {
    render(
      <MCPInventoryList
        servers={[]}
        composioConfigured={false}
        selectedId={null}
        onSelect={() => {}}
      />,
    );

    const setupBadge = screen.getByText("Setup Required");
    expect(setupBadge).toHaveClass("text-amber-900", "dark:text-amber-200");
    expect(setupBadge.parentElement).not.toHaveClass("opacity-80");
  });

  it("toggles family catalog browser on [+] button", async () => {
    render(<ProvidersSettingsPage />);

    // Switch to MCP Server tab
    fireEvent.click(await screen.findByRole("button", { name: /mcp server/i }));

    // Click [+] button in the inventory header
    const plusBtn = screen.getByLabelText(/browse provider families/i);
    fireEvent.click(plusBtn);

    // Verify catalog templates are displayed
    expect(await screen.findByText("Back to Inventory")).toBeInTheDocument();
    expect(screen.getByText("Composio Provider")).toBeInTheDocument();
    expect(screen.getByText("Remote MCP Gateway")).toBeInTheDocument();
    expect(screen.getByText("Local MCP Runtime")).toBeInTheDocument();
    expect(screen.getByText("Install & Use Tool Apps")).toBeInTheDocument();
  });
});

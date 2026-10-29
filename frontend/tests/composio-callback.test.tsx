import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { completeComposioOAuth, replace } = vi.hoisted(() => ({
  completeComposioOAuth: vi.fn(),
  replace: vi.fn(),
}));

const search = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  useSearchParams: () => search,
}));

vi.mock("@/lib/mcp-api", () => ({ completeComposioOAuth }));

import ComposioCallbackClient from "../app/dashboard/mcp/composio/callback/ComposioCallbackClient";

function resetSearch(): void {
  for (const key of [...search.keys()]) search.delete(key);
}

describe("Composio callback", () => {
  beforeEach(() => {
    completeComposioOAuth.mockReset();
    replace.mockReset();
    resetSearch();
  });

  it("verifies a successful Composio redirect and returns to the MCP page", async () => {
    search.set("toolkit_slug", "gmail");
    search.set("connected_account_id", "ca_123");
    search.set("status", "success");
    completeComposioOAuth.mockResolvedValue({ status: "connecting", server_id: "srv-1" });

    render(<ComposioCallbackClient />);

    await waitFor(() =>
      expect(completeComposioOAuth).toHaveBeenCalledWith({
        toolkitSlug: "gmail",
        connectedAccountId: "ca_123",
        status: "success",
      }),
    );
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith(
        "/dashboard/mcp?mcp_status=connected&server_id=srv-1",
      ),
    );
  });

  it("does not verify a redirect that is missing Composio parameters", async () => {
    search.set("toolkit_slug", "gmail");

    render(<ComposioCallbackClient />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /did not return a complete connection result/i,
    );
    expect(completeComposioOAuth).not.toHaveBeenCalled();
  });

  it("reports a canceled authorization without redirecting", async () => {
    search.set("toolkit_slug", "gmail");
    search.set("connected_account_id", "ca_123");
    search.set("status", "failed");
    completeComposioOAuth.mockResolvedValue({ status: "failed", server_id: "srv-1" });

    render(<ComposioCallbackClient />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/canceled or did not complete/i);
    expect(replace).not.toHaveBeenCalled();
  });

  it("reports a verification failure from the API", async () => {
    search.set("toolkit_slug", "gmail");
    search.set("connected_account_id", "ca_123");
    search.set("status", "success");
    completeComposioOAuth.mockRejectedValue(new Error("The Composio session does not match this app."));

    render(<ComposioCallbackClient />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /The Composio session does not match this app\./,
    );
    expect(replace).not.toHaveBeenCalled();
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../app/context/AuthContext";
import DeepSpaceChatClient from "../app/dashboard/deepspace/_components/DeepSpaceChatClient";

const fetchWithAuthMock = vi.fn();

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => window.location.pathname,
}));

vi.mock("@/lib/api", () => ({
  fetchWithAuth: (...args: unknown[]) => fetchWithAuthMock(...args),
  isDesktopEnvironment: () => false,
  getAccessTokenExpiry: () => null,
}));

vi.mock("@/lib/providers-api", () => ({
  listProviders: vi.fn(async () => []),
  listProviderModels: vi.fn(async () => []),
  refreshProviderModels: vi.fn(async () => []),
  listAssignments: vi.fn(async () => []),
  createAssignment: vi.fn(),
  updateAssignment: vi.fn(),
}));

vi.mock("@/lib/realtime", () => ({
  useRealtimeEvents: () => {},
}));

vi.mock("../app/dashboard/deepspace/_hooks/useDeepSpaceStream", () => ({
  useDeepSpaceStream: () => ({
    start: vi.fn(),
    cancel: vi.fn(),
    detach: vi.fn(),
    resume: vi.fn(),
  }),
}));

vi.mock("../app/dashboard/deepspace/_components/DeepSpaceComposer", () => ({
  default: () => <div data-testid="deepspace-composer" />,
}));

vi.mock("../app/dashboard/deepspace/_components/DeepSpaceScrollTracker", () => ({
  default: () => null,
}));

vi.mock("../app/components/dashboard/ChatSidebar", () => ({
  default: ({ children }: { children?: ReactNode }) => (
    <div data-testid="chat-sidebar">{children}</div>
  ),
}));

function jsonResponse(payload: unknown) {
  return { ok: true, status: 200, json: async () => payload };
}

function renderChat() {
  return render(
    <AuthProvider>
      <DeepSpaceChatClient activeConversationId="conv-1" isHistoryOpen />
    </AuthProvider>,
  );
}

/**
 * The start cards open the side panel on the surface the user picked, so the
 * workspace files, the terminal, and the browser are one click away.
 */
describe("DeepSpace start surfaces", () => {
  beforeEach(() => {
    fetchWithAuthMock.mockReset();
    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url.includes("/messages")) return jsonResponse({ messages: [] });
      if (url.includes("/workspace/tree")) return jsonResponse({ entries: [] });
      if (url.includes("/terminals")) return jsonResponse({ sessions: [] });
      return jsonResponse({});
    });
  });

  it("offers the workspace files, a terminal, and the browser", async () => {
    renderChat();

    expect(await screen.findByRole("button", { name: /Workspace files/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /New terminal/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Browser/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Agent Team" })).toBeInTheDocument();
  });

  it("opens the panel on the browser when the user picks the browser", async () => {
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: /Browser/ }));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "Browser" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });
  });

  it("opens the Agent Team overlay for the active conversation", async () => {
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: "Agent Team" }));

    expect(await screen.findByTestId("deepspace-agent-team-panel")).toBeInTheDocument();
    expect(fetchWithAuthMock).toHaveBeenCalledWith(
      "/deepspace/chats/conv-1/agent-team",
    );
  });

  it("opens the panel on the terminal when the user picks a terminal", async () => {
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: /New terminal/ }));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "Terminal" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });
  });

  it("starts a terminal session when the user picks one from the start cards", async () => {
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: /New terminal/ }));

    await waitFor(() => {
      expect(
        fetchWithAuthMock.mock.calls.some(
          ([url, init]) =>
            url === "/deepspace/chats/conv-1/terminals" && init?.method === "POST",
        ),
      ).toBe(true);
    });
  });

  it("opens the panel on the workspace files when the user picks the files", async () => {
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: /Workspace files/ }));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "Files" })).toHaveAttribute("aria-selected", "true");
    });
  });
});

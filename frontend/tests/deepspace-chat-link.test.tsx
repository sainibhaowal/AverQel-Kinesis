import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../app/context/AuthContext";
import DeepSpaceChatClient from "../app/dashboard/deepspace/_components/DeepSpaceChatClient";
import DeepSpacePageClient from "../app/dashboard/deepspace/_components/DeepSpacePageClient";

const fetchWithAuthMock = vi.fn();
const startMock = vi.fn();
const writeTextMock = vi.fn(async (_text: string) => {});

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
  useDeepSpaceStream: (options: {
    onEvent: (event: Record<string, unknown>) => void;
    onFinally?: () => void;
  }) => ({
    start: (...args: unknown[]) => startMock(options, ...args),
    cancel: vi.fn(),
    detach: vi.fn(),
    resume: vi.fn(),
  }),
}));

vi.mock("../app/dashboard/deepspace/_components/DeepSpaceComposer", () => ({
  default: ({ conversationId }: { conversationId?: string | null }) => (
    <div data-testid="deepspace-composer" data-conversation-id={conversationId ?? ""} />
  ),
}));

vi.mock("../app/dashboard/deepspace/_components/DeepSpaceThread", () => ({
  default: () => <div data-testid="deepspace-thread" />,
}));

vi.mock("../app/dashboard/deepspace/_components/DeepSpaceScrollTracker", () => ({
  default: () => null,
}));

// The history panel shows the actions it receives as children, so the stub
// keeps the copy action visible.
vi.mock("../app/components/dashboard/ChatSidebar", () => ({
  default: ({ children }: { children?: ReactNode }) => (
    <div data-testid="chat-sidebar">{children}</div>
  ),
}));

const conversations = [
  {
    id: "conv-1",
    title: "First conversation",
    updated_at: new Date().toISOString(),
    content_html: "",
  },
  {
    id: "conv-2",
    title: "Second conversation",
    updated_at: new Date().toISOString(),
    content_html: "",
  },
];

function renderPage() {
  return render(
    <AuthProvider>
      <DeepSpacePageClient />
    </AuthProvider>,
  );
}

function renderChat() {
  return render(
    <AuthProvider>
      <DeepSpaceChatClient activeConversationId="conv-1" isHistoryOpen />
    </AuthProvider>,
  );
}

describe("DeepSpace conversation links", () => {
  beforeEach(() => {
    fetchWithAuthMock.mockReset();
    startMock.mockReset();
    writeTextMock.mockClear();
    window.localStorage.clear();
    window.history.replaceState({}, "", "/dashboard/deepspace");
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: writeTextMock },
    });

    fetchWithAuthMock.mockImplementation(async (url: string) => {
      if (url === "/deepspace/chats") {
        return { ok: true, json: async () => ({ items: conversations }) };
      }
      if (url === "/deepspace/chats/conv-1/messages") {
        return {
          ok: true,
          json: async () => ({
            messages: [
              {
                id: "assistant-1",
                role: "assistant",
                content: "Saved DeepSpace answer.",
                created_at: new Date().toISOString(),
                metadata_json: {},
              },
            ],
          }),
        };
      }
      if (url === "/deepspace/chats/conv-1/queue") {
        return { ok: true, json: async () => [] };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  afterEach(() => {
    window.history.replaceState({}, "", "/dashboard/deepspace");
  });

  it("opens the conversation named by the link", async () => {
    window.history.replaceState({}, "", "/dashboard/deepspace?note=conv-1");

    renderPage();

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith("/deepspace/chats/conv-1/messages");
    });
    expect(screen.getByTestId("deepspace-composer")).toHaveAttribute(
      "data-conversation-id",
      "conv-1",
    );
    expect(fetchWithAuthMock).not.toHaveBeenCalledWith("/deepspace/chats/conv-2/messages");
  });

  it("copies a link to the current conversation from the history panel", async () => {
    renderChat();

    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));

    await waitFor(() => expect(writeTextMock).toHaveBeenCalledTimes(1));
    expect(writeTextMock.mock.calls[0]?.[0]).toBe(
      "http://localhost:3000/dashboard/deepspace?note=conv-1",
    );
    expect(await screen.findByText("Link copied")).toBeInTheDocument();
  });

  it("keeps DeepSpace app overlays attached to the window layer", async () => {
    render(
      <AuthProvider>
        <DeepSpaceChatClient activeConversationId="conv-1" />
      </AuthProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Jobs" }));

    const dialog = await screen.findByRole("dialog", { name: "Jobs" });
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    expect(screen.getByText("Background jobs and batch executions")).toBeInTheDocument();
  });

  it("leaves the page in its normal state for an unknown conversation", async () => {
    window.history.replaceState({}, "", "/dashboard/deepspace?note=missing-conversation");

    renderPage();

    await waitFor(() => {
      expect(fetchWithAuthMock.mock.calls.map(([url]) => url)).toContain("/deepspace/chats");
    });
    expect(
      fetchWithAuthMock.mock.calls.some(
        ([url]) => String(url).startsWith("/deepspace/chats/") && String(url).endsWith("/messages"),
      ),
    ).toBe(false);
    expect(screen.getByTestId("deepspace-composer")).toHaveAttribute("data-conversation-id", "");
    expect(screen.getByTestId("deepspace-thread")).toBeInTheDocument();
  });

  it("clears the copy confirmation on its own", async () => {
    vi.useFakeTimers();
    try {
      renderChat();

      fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText("Link copied")).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_000);
      });
      expect(screen.queryByText("Link copied")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

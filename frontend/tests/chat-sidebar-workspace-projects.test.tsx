import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ChatSidebar from "../app/components/dashboard/ChatSidebar";

const fetchWithAuthMock = vi.fn();
const promptMock = vi.hoisted(() => vi.fn());
const confirmMock = vi.hoisted(() => vi.fn());
const alertMock = vi.hoisted(() => vi.fn());

vi.mock("../lib/api", () => ({
  fetchWithAuth: (...args: unknown[]) => fetchWithAuthMock(...args),
}));

vi.mock("@/app/components/ui/AverQelDialogHost", () => ({
  averqelConfirm: (...args: unknown[]) => confirmMock(...args),
  averqelPrompt: (...args: unknown[]) => promptMock(...args),
  averqelAlert: (...args: unknown[]) => alertMock(...args),
}));

interface ProjectSession {
  id: string;
  title: string;
  updated_at: string;
}

interface TestProject {
  id: string;
  name: string;
  path: string;
  is_default: boolean;
  status: "ok" | "missing-dir";
  sessions: ProjectSession[];
}

const CONVERSATIONS = [
  { id: "conv-1", title: "Design notes", updated_at: "2026-11-10T10:00:00Z" },
  { id: "conv-2", title: "Release plan", updated_at: "2026-11-10T09:00:00Z" },
];

/**
 * Build one server projection of the project sidebar.
 *
 * @returns The mutable project payload the fetch mock serves.
 */
function workspaceProjects(): { workspaces: TestProject[] } {
  return {
    workspaces: [
      {
        id: "default",
        name: "Default",
        path: "/tmp/default",
        is_default: true,
        status: "ok",
        sessions: [
          {
            id: "conv-2",
            title: "Release plan",
            updated_at: "2026-11-10T09:00:00Z",
          },
        ],
      },
      {
        id: "reports",
        name: "Reports",
        path: "/tmp/reports",
        is_default: false,
        status: "missing-dir",
        sessions: [
          {
            id: "conv-1",
            title: "Design notes",
            updated_at: "2026-11-10T10:00:00Z",
          },
        ],
      },
    ],
  };
}

const projectsRef = { current: workspaceProjects() };

function jsonResponse(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response;
}

/**
 * Render the sidebar with the project grouping enabled.
 */
function renderGroupedSidebar() {
  return render(
    <ChatSidebar
      endpointBase="/deepspace/chats"
      enableWorkspaceGroups
      currentConversationId="conv-1"
      onSelectConversation={() => {}}
      onNewChat={() => {}}
    />,
  );
}

describe("chat sidebar workspace projects", () => {
  beforeEach(() => {
    fetchWithAuthMock.mockReset();
    promptMock.mockReset();
    confirmMock.mockReset();
    alertMock.mockReset();
    confirmMock.mockResolvedValue(true);
    projectsRef.current = workspaceProjects();
    fetchWithAuthMock.mockImplementation(
      async (url: string, options?: { method?: string; body?: string }) => {
        if (url === "/deepspace/chats") {
          return jsonResponse({ items: CONVERSATIONS });
        }
        if (url === "/deepspace/chats/workspaces/projects") {
          return jsonResponse(projectsRef.current);
        }
        if (url === "/deepspace/chats/workspaces/reports" && options?.method === "PATCH") {
          return jsonResponse(projectsRef.current.workspaces[1]);
        }
        if (url === "/deepspace/chats/workspaces/reports/sessions/order") {
          return jsonResponse({ workspace: "reports", conversation_ids: [] });
        }
        if (url === "/deepspace/chats/conv-1/workspace" && options?.method === "PUT") {
          return jsonResponse({ workspace: "reports" });
        }
        if (url === "/deepspace/chats/conv-2/workspace" && options?.method === "PUT") {
          return jsonResponse({ workspace: "reports" });
        }
        return jsonResponse({});
      },
    );
  });

  it("groups conversations under projects and flags a missing directory", async () => {
    renderGroupedSidebar();

    expect(await screen.findByText("Design notes")).toBeInTheDocument();

    const reportsGroup = screen.getByTestId("workspace-project-reports");
    const defaultGroup = screen.getByTestId("workspace-project-default");
    expect(within(reportsGroup).getByText("Reports")).toBeInTheDocument();
    expect(within(reportsGroup).getByText("Design notes")).toBeInTheDocument();
    expect(within(reportsGroup).getByText("Folder missing")).toBeInTheDocument();
    expect(within(defaultGroup).getByText("Release plan")).toBeInTheDocument();

    fireEvent.click(
      within(reportsGroup).getByRole("button", { name: /collapse reports workspace/i }),
    );
    expect(screen.queryByText("Design notes")).not.toBeInTheDocument();
  });

  it("renames a project without changing its conversations", async () => {
    promptMock.mockResolvedValueOnce("Research");
    renderGroupedSidebar();
    expect(await screen.findByText("Design notes")).toBeInTheDocument();

    // Update the ref before the rename so the mock returns the new name
    projectsRef.current.workspaces[1].name = "Research";

    fireEvent.click(screen.getByRole("button", { name: "Rename Reports" }));

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith("/deepspace/chats/workspaces/reports", {
        method: "PATCH",
        body: JSON.stringify({ name: "Research" }),
      });
    });
    // Wait for the fetchProjects call to complete (4 calls: chats, projects, PATCH, projects)
    await waitFor(() =>
      expect(fetchWithAuthMock).toHaveBeenCalledTimes(4),
    );
    await waitFor(() => expect(screen.getByText("Research")).toBeInTheDocument());
    expect(screen.getByText("Design notes")).toBeInTheDocument();
  });

  it("moves a project before its neighbour in the user's order", async () => {
    projectsRef.current = {
      workspaces: [
        ...projectsRef.current.workspaces,
        {
          id: "notes",
          name: "Notes",
          path: "/tmp/notes",
          is_default: false,
          status: "ok",
          sessions: [],
        },
      ],
    };
    renderGroupedSidebar();
    expect(await screen.findByText("Notes")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Move Notes up" }));

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith("/deepspace/chats/workspaces/notes/order", {
        method: "PUT",
        body: JSON.stringify({ before: "reports" }),
      });
    });
  });

  it("moves a conversation to another project by dragging it", async () => {
    renderGroupedSidebar();
    expect(await screen.findByText("Design notes")).toBeInTheDocument();

    const dataTransfer = {
      effectAllowed: "",
      dropEffect: "",
      setData: vi.fn(),
    };
    fireEvent.dragStart(screen.getByText("Design notes"), { dataTransfer });
    const notesGroup = screen.getByTestId("workspace-project-default");
    fireEvent.dragOver(notesGroup, { dataTransfer });
    fireEvent.drop(notesGroup, { dataTransfer });

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith("/deepspace/chats/conv-1/workspace", {
        method: "PUT",
        body: JSON.stringify({ workspace: "default" }),
      });
    });
  });

  it("orders conversations inside one project by dragging", async () => {
    projectsRef.current = {
      workspaces: [
        {
          ...projectsRef.current.workspaces[0],
          sessions: [],
        },
        {
          ...projectsRef.current.workspaces[1],
          status: "ok",
          sessions: [
            { id: "conv-1", title: "Design notes", updated_at: "2026-11-10T10:00:00Z" },
            { id: "conv-2", title: "Release plan", updated_at: "2026-11-10T09:00:00Z" },
          ],
        },
      ],
    };
    renderGroupedSidebar();
    expect(await screen.findByText("Design notes")).toBeInTheDocument();

    const dataTransfer = {
      effectAllowed: "",
      dropEffect: "",
      setData: vi.fn(),
    };
    fireEvent.dragStart(screen.getByText("Release plan"), { dataTransfer });
    fireEvent.drop(screen.getByText("Design notes"), { dataTransfer });

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith(
        "/deepspace/chats/workspaces/reports/sessions/order",
        {
          method: "PUT",
          body: JSON.stringify({ conversation_ids: ["conv-2", "conv-1"] }),
        },
      );
    });
  });

  it("retries a guarded delete after closing live terminals", async () => {
    let deletions = 0;
    fetchWithAuthMock.mockImplementation(
      async (url: string, options?: { method?: string; body?: string }) => {
        if (url === "/deepspace/chats") {
          return jsonResponse({ items: CONVERSATIONS });
        }
        if (url === "/deepspace/chats/workspaces/projects") {
          return jsonResponse(projectsRef.current);
        }
        if (
          options?.method === "DELETE" &&
          url.startsWith("/deepspace/chats/workspaces/reports")
        ) {
          deletions += 1;
          if (!url.includes("stop_sessions=true")) {
            return jsonResponse(
              {
                error: {
                  message:
                    "This workspace is still in use: a terminal session is live. Stop that work or close its terminal sessions before removing the workspace.",
                },
              },
              409,
            );
          }
          return jsonResponse({ deleted: true, id: "reports" });
        }
        return jsonResponse({});
      },
    );
    renderGroupedSidebar();
    expect(await screen.findByText("Reports")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove Reports" }));

    await waitFor(() => {
      expect(fetchWithAuthMock).toHaveBeenCalledWith(
        "/deepspace/chats/workspaces/reports?stop_sessions=true",
        { method: "DELETE" },
      );
    });
    expect(deletions).toBe(2);
    expect(confirmMock).toHaveBeenCalledTimes(2);
  });
});

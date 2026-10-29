import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchWithAuth = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  fetchWithAuth,
}));

import DeepSpaceAgentTeamPanel from "../app/dashboard/deepspace/_components/DeepSpaceAgentTeamPanel";

function jsonResponse(payload: unknown) {
  return { ok: true, status: 200, json: async () => payload };
}

describe("DeepSpaceAgentTeamPanel", () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
    fetchWithAuth.mockResolvedValue(
      jsonResponse({
        enabled: true,
        preset: "custom",
        team_tools_available: true,
        team_tools: [{ name: "spawn_teammate", allowed: true }],
        max_members: 8,
        lead: { name: "lead", label: "DeepSpace lead", kind: "lead" },
        members: [
          {
            id: "member-1",
            name: "researcher",
            provider: "deepseek",
            model: "deepseek-flash",
            tool_filter: ["web_search"],
            state: "running",
            child_id: "01923456-789a-bcde-f012-3456789abcde",
            result: "Checking the public sources.",
            created_at: "2026-10-28T10:00:00Z",
          },
        ],
        tasks: [
          {
            id: "task-1",
            title: "Verify sources",
            body: "### GOAL\nConfirm each source before reporting.\n\n### RUBRIC\n1. Check veracity",
            status: "in_progress",
            owner: "researcher",
            revision: 2,
            depends_on: [],
            write_scopes: ["research/scout/"],
            ready: true,
            blocked_by: [],
            write_scope_warnings: [],
            updated_at: "2026-10-28T10:01:00Z",
          },
          {
            id: "task-2",
            title: "Draft synthesis",
            body: "Draft final response.",
            status: "pending",
            owner: null,
            revision: 1,
            depends_on: ["task-1"],
            write_scopes: ["research/scout/summary.md"],
            ready: false,
            blocked_by: ["task-1"],
            write_scope_warnings: ["Verify sources (task-1)"],
            updated_at: "2026-10-28T10:02:00Z",
          },
        ],
        messages: [
          {
            id: "message-1",
            from: "lead",
            to: "researcher",
            body: "Please verify the sources.",
            state: "delivered",
            created_at: "2026-10-28T10:02:00Z",
          },
        ],
        children: [
          {
            agent_id: "child-1",
            label: "researcher",
            origin: "subagent",
            mode: "continuable",
            status: "running",
            depth: 1,
            turn_count: 1,
            pending_messages: 0,
            agent_provider: "deepseek",
            agent_model: "deepseek-flash",
          },
        ],
      }),
    );
  });

  it("renders the live members, tasks with write scopes, and messages", async () => {
    render(<DeepSpaceAgentTeamPanel conversationId="conversation-1" />);

    expect((await screen.findAllByText("researcher")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Verify sources")).toBeInTheDocument();
    expect(screen.getByText("Draft synthesis")).toBeInTheDocument();
    expect(screen.getByText("Delegated Children")).toBeInTheDocument();
    expect(screen.getAllByText(/Write scopes:/).length).toBe(2);
    expect(screen.getByText(/Overlap warning: Verify sources/)).toBeInTheDocument();
    expect(screen.getByText("Blocked (task-1)")).toBeInTheDocument();

    await waitFor(() => {
      expect(fetchWithAuth).toHaveBeenCalledWith(
        "/deepspace/chats/conversation-1/agent-team",
      );
    });
  });

  it("toggles enable and disable using the wired button", async () => {
    fetchWithAuth.mockResolvedValueOnce(
      jsonResponse({
        enabled: true,
        preset: "custom",
        team_tools_available: true,
        team_tools: [],
        max_members: 8,
        lead: { name: "lead", label: "DeepSpace lead", kind: "lead" },
        members: [],
        tasks: [],
        messages: [],
        children: [],
      }),
    );

    render(<DeepSpaceAgentTeamPanel conversationId="conversation-1" />);

    const toggleButton = await screen.findByTestId("agent-team-toggle-button");
    expect(toggleButton).toHaveTextContent("Disable Team");

    fetchWithAuth.mockResolvedValueOnce(
      jsonResponse({
        enabled: false,
        preset: "custom",
        team_tools_available: true,
        team_tools: [],
        max_members: 8,
        lead: { name: "lead", label: "DeepSpace lead", kind: "lead" },
        members: [],
        tasks: [],
        messages: [],
        children: [],
      }),
    );

    fireEvent.click(toggleButton);

    await waitFor(() => {
      expect(fetchWithAuth).toHaveBeenCalledWith(
        "/deepspace/chats/conversation-1/agent-team",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ enabled: false }),
        }),
      );
    });
  });

  it("allows clicking teammate to open session inspector and triggers onSelectSession", async () => {
    const onSelectSession = vi.fn();
    render(
      <DeepSpaceAgentTeamPanel
        conversationId="conversation-1"
        onSelectSession={onSelectSession}
      />,
    );

    const teammateCard = await screen.findByRole("button", {
      name: /View live session of teammate researcher/,
    });
    fireEvent.click(teammateCard);

    expect(onSelectSession).toHaveBeenCalledWith("01923456-789a-bcde-f012-3456789abcde");
    expect(screen.getByText("Session / Child ID:")).toBeInTheDocument();
    expect(screen.getByText("Interrupt Teammate")).toBeInTheDocument();
  });

  it("expands task to view full markdown instructions", async () => {
    render(<DeepSpaceAgentTeamPanel conversationId="conversation-1" />);

    const expandBtns = await screen.findAllByRole("button", {
      name: "Expand task details",
    });
    fireEvent.click(expandBtns[0]);

    expect(screen.getByText("GOAL")).toBeInTheDocument();
    expect(screen.getByText("RUBRIC")).toBeInTheDocument();
  });

  it("explains that a conversation has no named members yet", async () => {
    fetchWithAuth.mockResolvedValueOnce(
      jsonResponse({
        enabled: true,
        preset: "custom",
        team_tools_available: true,
        team_tools: [],
        max_members: 8,
        lead: { name: "lead", label: "DeepSpace lead", kind: "lead" },
        members: [],
        tasks: [],
        messages: [],
        children: [],
      }),
    );

    render(<DeepSpaceAgentTeamPanel conversationId="conversation-1" />);

    expect(
      await screen.findByText(/No named teammates yet\. The lead creates them/),
    ).toBeInTheDocument();
  });
});

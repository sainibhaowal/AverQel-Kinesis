import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DeepSpaceAgentTree, {
  buildAgentTree,
} from "../app/dashboard/deepspace/_components/DeepSpaceAgentTree";
import DeepSpacePlanReviewCard from "../app/dashboard/deepspace/_components/DeepSpacePlanReviewCard";

const fetchWithAuth = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  fetchWithAuth,
}));

describe("the DeepSpace plan review card", () => {
  it("shows the plan and offers the two decisions", () => {
    const onApprove = vi.fn();
    const onRequestChanges = vi.fn();

    render(
      <DeepSpacePlanReviewCard
        planId="plan-1"
        title="Ship the release"
        content={"# Plan\n\n1. Build\n2. Test"}
        onApprove={onApprove}
        onRequestChanges={onRequestChanges}
      />,
    );

    expect(screen.getByText("Ship the release")).toBeInTheDocument();
    expect(screen.getByText(/# Plan/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve plan/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /request changes/i })).toBeInTheDocument();
  });

  it("approves the plan the user selected", async () => {
    const onApprove = vi.fn();

    render(
      <DeepSpacePlanReviewCard
        planId="plan-1"
        title="Ship the release"
        content="# Plan"
        onApprove={onApprove}
        onRequestChanges={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /approve plan/i }));

    await waitFor(() => {
      expect(onApprove).toHaveBeenCalledWith("plan-1");
    });
  });

  it("sends the feedback the user typed with the change request", async () => {
    const onRequestChanges = vi.fn();

    render(
      <DeepSpacePlanReviewCard
        planId="plan-2"
        title="Ship the release"
        content="# Plan"
        onApprove={vi.fn()}
        onRequestChanges={onRequestChanges}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /request changes/i }));
    fireEvent.change(screen.getByLabelText(/feedback for the plan/i), {
      target: { value: "Add a rollback step." },
    });
    fireEvent.click(screen.getByRole("button", { name: /send feedback/i }));

    await waitFor(() => {
      expect(onRequestChanges).toHaveBeenCalledWith("plan-2", "Add a rollback step.");
    });
  });
});

describe("the DeepSpace agent tree", () => {
  it("builds a tree from the flat projection rows", () => {
    const tree = buildAgentTree([
      {
        agent_id: "child-1",
        parent_agent_id: null,
        label: "Research",
        mode: "continuable",
        origin: "subagent",
        status: "idle",
        depth: 1,
        turn_count: 2,
      },
      {
        agent_id: "child-2",
        parent_agent_id: "child-1",
        label: "Summarize",
        mode: "one-shot",
        origin: "subagent",
        status: "completed",
        depth: 2,
        turn_count: 1,
      },
    ]);

    expect(tree).toHaveLength(1);
    expect(tree[0].label).toBe("Research");
    expect(tree[0].children).toHaveLength(1);
    expect(tree[0].children[0].label).toBe("Summarize");
  });

  it("shows the delegation tree the projection reports", async () => {
    fetchWithAuth.mockResolvedValue({
      ok: true,
      json: async () => ({
        delegation_tree: [
          {
            agent_id: "child-1",
            parent_agent_id: null,
            label: "Research the sources",
            mode: "continuable",
            origin: "subagent",
            status: "idle",
            depth: 1,
            turn_count: 3,
          },
        ],
      }),
    });

    render(<DeepSpaceAgentTree conversationId="conversation-1" open />);

    await waitFor(() => {
      expect(screen.getByText("Research the sources")).toBeInTheDocument();
    });
    expect(fetchWithAuth).toHaveBeenCalledWith("/deepspace/chats/conversation-1/projection");
  });

  it("reports a conversation without delegated agents", async () => {
    fetchWithAuth.mockResolvedValue({
      ok: true,
      json: async () => ({ delegation_tree: [] }),
    });

    render(<DeepSpaceAgentTree conversationId="conversation-2" open />);

    await waitFor(() => {
      expect(screen.getByText(/has not delegated any work/i)).toBeInTheDocument();
    });
  });
});

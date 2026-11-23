import { describe, expect, it, vi } from "vitest";

import { fetchWithAuth } from "@/lib/api";
import { fetchConversationGoals, runGoalCommand } from "../app/dashboard/deepspace/_lib/goal-api";

vi.mock("@/lib/api", () => ({
  fetchWithAuth: vi.fn(),
}));

describe("DeepSpace goal API", () => {
  it("reads a conversation's goals", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(
      new Response(JSON.stringify([{ goal_id: "goal-1", name: "ship-parity" }]), {
        status: 200,
      }),
    );

    await expect(fetchConversationGoals("conv-1")).resolves.toEqual([
      { goal_id: "goal-1", name: "ship-parity" },
    ]);

    expect(fetchWithAuth).toHaveBeenCalledWith("/deepspace/chats/conv-1/goals", {
      method: "GET",
    });
  });

  it("returns no goals when the backend sends a payload that is not a list", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(
      new Response(JSON.stringify({ goals: [] }), { status: 200 }),
    );

    await expect(fetchConversationGoals("conv-1")).resolves.toEqual([]);
  });

  it("reports a rejected read", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(new Response(null, { status: 500 }));

    await expect(fetchConversationGoals("conv-1")).rejects.toThrow("HTTP 500");
  });

  it("runs a goal command", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          success: true,
          message: "Goal recorded: ship-parity",
          error_code: null,
          data: { action: "goal_create" },
        }),
        { status: 200 },
      ),
    );

    const result = await runGoalCommand("conv-1", "ship the migration");

    expect(result.success).toBe(true);
    expect(result.data.action).toBe("goal_create");
    expect(fetchWithAuth).toHaveBeenCalledWith("/deepspace/chats/conv-1/goals/command", {
      method: "POST",
      body: JSON.stringify({ input: "ship the migration" }),
    });
  });

  it("names the goal a control word acts on", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          success: true,
          message: "Goal paused: ship-parity",
          error_code: null,
          data: { action: "goal_pause" },
        }),
        { status: 200 },
      ),
    );

    const result = await runGoalCommand("conv-1", "pause", "ship-parity");

    expect(result.success).toBe(true);
    expect(fetchWithAuth).toHaveBeenCalledWith("/deepspace/chats/conv-1/goals/command", {
      method: "POST",
      body: JSON.stringify({ input: "pause", goal_name: "ship-parity" }),
    });
  });

  it("reports a rejected command", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(new Response(null, { status: 422 }));

    await expect(runGoalCommand("conv-1", "pause")).rejects.toThrow("HTTP 422");
  });

  it("escapes the conversation id in the request path", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(new Response("[]", { status: 200 }));

    await fetchConversationGoals("conv/1");

    expect(fetchWithAuth).toHaveBeenCalledWith("/deepspace/chats/conv%2F1/goals", {
      method: "GET",
    });
  });
});

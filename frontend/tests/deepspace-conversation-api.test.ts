import { describe, expect, it, vi } from "vitest";

import { fetchWithAuth } from "@/lib/api";
import { forkConversation } from "../app/dashboard/deepspace/_lib/conversation-api";

vi.mock("@/lib/api", () => ({
  fetchWithAuth: vi.fn(),
}));

describe("DeepSpace conversation fork API", () => {
  it("routes a message boundary and copy policy to the backend", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(
      new Response(JSON.stringify({ id: "fork-1", title: "Source (fork)" }), { status: 200 }),
    );

    await expect(
      forkConversation("source-1", { messageId: "message-2", copyFiles: true }),
    ).resolves.toEqual({ id: "fork-1", title: "Source (fork)" });

    expect(fetchWithAuth).toHaveBeenCalledWith("/deepspace/chats/source-1/fork", {
      method: "POST",
      body: JSON.stringify({ message_id: "message-2", copy_files: true }),
    });
  });

  it("returns a failure when the backend rejects the fork", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(new Response(null, { status: 422 }));

    await expect(forkConversation("source-1")).rejects.toThrow("HTTP 422");
  });
});

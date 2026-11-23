import { createRef } from "react";
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchWithAuth } = vi.hoisted(() => ({ fetchWithAuth: vi.fn() }));

vi.mock("@/lib/api", () => ({ fetchWithAuth }));

import DeepSpaceSlashMenu from "../app/dashboard/deepspace/_components/DeepSpaceSlashMenu";

function renderMenu(isOpen: boolean) {
  return render(
    <DeepSpaceSlashMenu
      isOpen={isOpen}
      onClose={vi.fn()}
      onExecute={vi.fn()}
      conversationId="conversation-1"
      inputValue=""
      cursorPosition={0}
      textareaRef={createRef<HTMLTextAreaElement>()}
    />,
  );
}

describe("DeepSpaceSlashMenu command loading", () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
    fetchWithAuth.mockResolvedValue({ ok: true, json: async () => [] });
  });

  it("does not fetch dynamic commands while closed", async () => {
    await act(async () => {
      renderMenu(false);
    });

    expect(fetchWithAuth).not.toHaveBeenCalled();
  });

  it("loads dynamic commands when opened for a conversation", async () => {
    renderMenu(true);

    await waitFor(() => expect(fetchWithAuth).toHaveBeenCalledWith("/host/slash-commands"));
  });
});

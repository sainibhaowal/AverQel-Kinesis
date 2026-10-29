import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import DeepSpaceComposer from "../app/dashboard/deepspace/_components/DeepSpaceComposer";

/**
 * The composer's clipboard button reads what the system clipboard holds:
 * an image becomes an attachment, and text becomes part of the message.
 */
describe("DeepSpaceComposer clipboard button", () => {
  const readMock = vi.fn();
  const readTextMock = vi.fn();

  function renderComposer(onQueryChange = vi.fn()) {
    return render(
      <DeepSpaceComposer
        query=""
        conversationId="conv-1"
        isStreaming={false}
        onQueryChange={onQueryChange}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
      />,
    );
  }

  it("offers the button and names it", () => {
    renderComposer();

    const button = screen.getByRole("button", { name: "Paste from the clipboard" });
    expect(button).toBeInTheDocument();
    expect(button).not.toHaveAttribute("title");
  });

  it("puts the clipboard's text into the message", async () => {
    readMock.mockResolvedValue([]);
    readTextMock.mockResolvedValue("a note from the clipboard");
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { read: readMock, readText: readTextMock },
    });
    const onQueryChange = vi.fn();
    renderComposer(onQueryChange);

    fireEvent.click(screen.getByRole("button", { name: "Paste from the clipboard" }));

    await waitFor(() => {
      expect(onQueryChange).toHaveBeenCalledWith("a note from the clipboard");
    });
  });

  it("says so when the browser refuses the clipboard", async () => {
    readMock.mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { read: readMock, readText: readTextMock },
    });
    renderComposer();

    fireEvent.click(screen.getByRole("button", { name: "Paste from the clipboard" }));

    expect(await screen.findByText(/clipboard could not be read/i)).toBeInTheDocument();
  });

  it("says so when the browser cannot read the clipboard at all", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    renderComposer();

    fireEvent.click(screen.getByRole("button", { name: "Paste from the clipboard" }));

    expect(await screen.findByText(/does not let the page read the clipboard/i)).toBeInTheDocument();
  });
});

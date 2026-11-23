import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import DeepSpaceComposer from "../app/dashboard/deepspace/_components/DeepSpaceComposer";
import {
  ASK_DEEPSPACE_EVENT,
  type AskDeepSeekDetail,
} from "../app/dashboard/deepspace/_lib/ask-selection";

function renderComposer(query = "") {
  const onQueryChange = vi.fn();
  const onSubmit = vi.fn();
  const view = render(
    <DeepSpaceComposer
      query={query}
      conversationId="conv-1"
      isStreaming={false}
      onQueryChange={onQueryChange}
      onSubmit={onSubmit}
      onStop={vi.fn()}
    />,
  );
  return { ...view, onQueryChange, onSubmit };
}

afterEach(() => {
  delete window.electron;
});

function askSelection(text: string, source = "tooltip") {
  act(() => {
    window.dispatchEvent(
      new CustomEvent<AskDeepSeekDetail>(ASK_DEEPSPACE_EVENT, {
        detail: { text, source },
      }),
    );
  });
}

describe("DeepSpaceComposer Ask DeepSeek insertion", () => {
  it("adds the selected text as a quote without submitting", async () => {
    const { onQueryChange, onSubmit } = renderComposer();

    askSelection("  selected\r\ntext  ");

    expect(onQueryChange).toHaveBeenCalledWith("> selected\n> text\n\n");
    expect(onSubmit).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByPlaceholderText("Message DeepSpace...")).toHaveFocus();
    });
  });

  it("keeps an existing prompt and separates the quote", () => {
    const { onQueryChange } = renderComposer("Explain this:");

    askSelection("selected");

    expect(onQueryChange).toHaveBeenCalledWith("Explain this:\n\n> selected\n\n");
  });

  it("accepts the native Electron menu source", () => {
    const { onQueryChange } = renderComposer();

    askSelection("native selection", "desktop-menu");

    expect(onQueryChange).toHaveBeenCalledWith("> native selection\n\n");
  });

  it("ignores an empty selection", () => {
    const { onQueryChange } = renderComposer("existing");

    askSelection("   ");

    expect(onQueryChange).not.toHaveBeenCalled();
  });

  it("does not submit when the composer already has focus", () => {
    const { onQueryChange, onSubmit } = renderComposer();
    const textarea = screen.getByPlaceholderText("Message DeepSpace...");
    fireEvent.focus(textarea);

    askSelection("focused quote");

    expect(onQueryChange).toHaveBeenCalledWith("> focused quote\n\n");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("tells Electron when a composer can receive Ask DeepSeek", () => {
    const setAskDeepSeekAvailable = vi.fn();
    window.electron = {
      isElectron: true,
      contextMenu: {
        setAskDeepSeekAvailable,
        onAskDeepSeek: () => () => undefined,
      },
    } as unknown as Window["electron"];

    const { unmount } = renderComposer();

    expect(setAskDeepSeekAvailable).toHaveBeenCalledTimes(1);
    expect(setAskDeepSeekAvailable).toHaveBeenCalledWith(true);

    unmount();

    expect(setAskDeepSeekAvailable).toHaveBeenLastCalledWith(false);
  });
});

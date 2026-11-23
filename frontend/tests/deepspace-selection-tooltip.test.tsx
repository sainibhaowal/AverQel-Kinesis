import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import DeepSpaceSelectionTooltip from "../app/dashboard/deepspace/_components/DeepSpaceSelectionTooltip";
import { ASK_DEEPSPACE_EVENT } from "../app/dashboard/deepspace/_lib/ask-selection";

function makeSurface(text: string) {
  const surface = document.createElement("div");
  surface.setAttribute("data-deepspace-selection-surface", "");
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  surface.append(paragraph);
  document.body.append(surface);
  return { surface, paragraph };
}

function fakeSelection(text: string, anchorNode: Node | null): Selection {
  let current = text;
  return {
    get isCollapsed() {
      return current.length === 0;
    },
    get rangeCount() {
      return current.length === 0 ? 0 : 1;
    },
    anchorNode,
    focusNode: anchorNode,
    toString: () => current,
    getRangeAt: () => ({
      getBoundingClientRect: () => ({
        top: 120,
        left: 40,
        right: 240,
        bottom: 150,
        width: 200,
        height: 30,
      }),
    }),
    removeAllRanges: () => {
      current = "";
    },
  } as unknown as Selection;
}

async function showTooltipFor(selection: Selection) {
  const getSelection = vi.spyOn(window, "getSelection").mockReturnValue(selection);
  const view = render(<DeepSpaceSelectionTooltip />);
  fireEvent.mouseUp(window);
  const button = await screen.findByRole("button", {
    name: "Ask DeepSeek about the selected text",
  });
  return { ...view, button, getSelection };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
  delete window.electron;
});

describe("DeepSpaceSelectionTooltip", () => {
  it("appears for text selected in the DeepSpace surface and inserts it without submitting", async () => {
    const { surface, paragraph } = makeSurface("quoted answer");
    const listener = vi.fn();
    window.addEventListener(ASK_DEEPSPACE_EVENT, listener);
    const selection = fakeSelection("  quoted\r\nanswer  ", paragraph.firstChild);
    const removeAllRanges = vi.spyOn(selection, "removeAllRanges");

    const { button } = await showTooltipFor(selection);
    expect(button).toBeInTheDocument();

    fireEvent.click(button);

    expect(listener).toHaveBeenCalledTimes(1);
    const event = listener.mock.calls[0][0] as CustomEvent;
    expect(event.detail).toEqual({
      text: "quoted\nanswer",
      source: "tooltip",
    });
    expect(removeAllRanges).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByRole("button", { name: "Ask DeepSeek about the selected text" }),
    ).toBeNull();

    // A refresh scheduled before the click must not bring the consumed action
    // back now that the underlying selection has been cleared.
    fireEvent.mouseUp(window);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 120));
    });
    expect(
      screen.queryByRole("button", { name: "Ask DeepSeek about the selected text" }),
    ).toBeNull();

    window.removeEventListener(ASK_DEEPSPACE_EVENT, listener);
    surface.remove();
  });

  it("does not appear for a selection inside the composer's textarea", async () => {
    const { surface } = makeSurface("");
    const textarea = document.createElement("textarea");
    textarea.value = "composer text";
    surface.append(textarea);
    const getSelection = vi
      .spyOn(window, "getSelection")
      .mockReturnValue(fakeSelection("composer text", textarea));

    render(<DeepSpaceSelectionTooltip />);
    fireEvent.mouseUp(window);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 120));
    });

    expect(
      screen.queryByRole("button", { name: "Ask DeepSeek about the selected text" }),
    ).toBeNull();
    expect(getSelection).toHaveBeenCalled();
    surface.remove();
  });

  it("does not appear for a selection outside DeepSpace", async () => {
    const outside = document.createElement("p");
    outside.textContent = "outside";
    document.body.append(outside);
    vi.spyOn(window, "getSelection").mockReturnValue(fakeSelection("outside", outside.firstChild));

    render(<DeepSpaceSelectionTooltip />);
    fireEvent.mouseUp(window);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 120));
    });

    expect(
      screen.queryByRole("button", { name: "Ask DeepSeek about the selected text" }),
    ).toBeNull();
    outside.remove();
  });

  it("forwards selections sent by the native Electron Ask DeepSeek item", async () => {
    let nativeCallback: ((text: string) => void) | undefined;
    const unsubscribe = vi.fn();
    window.electron = {
      isElectron: true,
      contextMenu: {
        onAskDeepSeek: (callback: (text: string) => void) => {
          nativeCallback = callback;
          return unsubscribe;
        },
      },
    } as unknown as Window["electron"];

    const listener = vi.fn();
    window.addEventListener(ASK_DEEPSPACE_EVENT, listener);
    const view = render(<DeepSpaceSelectionTooltip />);

    expect(nativeCallback).toBeTypeOf("function");
    act(() => nativeCallback?.("from the native menu"));

    expect(listener).toHaveBeenCalledTimes(1);
    const event = listener.mock.calls[0][0] as CustomEvent;
    expect(event.detail).toEqual({
      text: "from the native menu",
      source: "desktop-menu",
    });

    view.unmount();
    expect(unsubscribe).toHaveBeenCalledTimes(1);
    window.removeEventListener(ASK_DEEPSPACE_EVENT, listener);
  });
});

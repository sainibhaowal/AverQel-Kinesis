import { describe, expect, it, vi } from "vitest";

import {
  ASK_DEEPSPACE_EVENT,
  COMPOSER_MAX_LENGTH,
  dispatchAskDeepSeek,
  insertSelectionIntoQuery,
  normalizeSelectionText,
  readAskSelection,
  selectionIsInSurface,
} from "../app/dashboard/deepspace/_lib/ask-selection";

function fakeSelection({
  text,
  anchorNode,
  rect = { top: 10, left: 20, right: 120, bottom: 40, width: 100, height: 30 },
  rangeCount = 1,
}: {
  text: string;
  anchorNode: Node | null;
  rect?: {
    top: number;
    left: number;
    right: number;
    bottom: number;
    width: number;
    height: number;
  };
  rangeCount?: number;
}): Selection {
  return {
    isCollapsed: text.length === 0,
    rangeCount,
    anchorNode,
    focusNode: anchorNode,
    toString: () => text,
    getRangeAt: () => ({
      getBoundingClientRect: () => rect,
    }),
  } as unknown as Selection;
}

describe("normalizeSelectionText", () => {
  it("normalizes newlines and trims the edges", () => {
    expect(normalizeSelectionText("  first\r\nsecond  ")).toBe("first\nsecond");
    expect(normalizeSelectionText("\u0000quoted\u0000")).toBe("quoted");
  });

  it("rejects empty and non-string values", () => {
    expect(normalizeSelectionText("")).toBe("");
    expect(normalizeSelectionText("   ")).toBe("");
    expect(normalizeSelectionText(null)).toBe("");
    expect(normalizeSelectionText(42)).toBe("");
  });

  it("caps long selections with an ellipsis", () => {
    const result = normalizeSelectionText("x".repeat(50), 20);
    expect(result).toHaveLength(20);
    expect(result.endsWith("…")).toBe(true);
  });
});

describe("insertSelectionIntoQuery", () => {
  it("adds an empty composer's selection as a quote and leaves room for a question", () => {
    const result = insertSelectionIntoQuery("", "first line\r\nsecond line");

    expect(result.inserted).toBe(true);
    expect(result.query).toBe("> first line\n> second line\n\n");
    expect(result.cursor).toBe(result.query.length);
  });

  it("keeps existing text and separates the quote with blank lines", () => {
    const result = insertSelectionIntoQuery("Explain this:", "selected");

    expect(result.query).toBe("Explain this:\n\n> selected\n\n");
    expect(result.cursor).toBe(result.query.length);
  });

  it("replaces a focused textarea range instead of appending", () => {
    const result = insertSelectionIntoQuery("before KEEP after", "quote", {
      start: 7,
      end: 11,
    });

    expect(result.query).toBe("before \n\n> quote\n\n after");
    expect(result.cursor).toBe("before \n\n> quote\n\n".length);
  });

  it("never exceeds the composer limit", () => {
    const result = insertSelectionIntoQuery("question", "y".repeat(500), {
      maxLength: 40,
    });

    expect(result.inserted).toBe(true);
    expect(result.query.length).toBeLessThanOrEqual(40);
    expect(result.cursor).toBeLessThanOrEqual(result.query.length);
  });

  it("leaves the composer alone when there is nothing to insert", () => {
    const result = insertSelectionIntoQuery("existing", "   ");
    expect(result).toEqual({ query: "existing", cursor: 8, inserted: false });
  });
});

describe("dispatchAskDeepSeek", () => {
  it("broadcasts normalized text to composer listeners", () => {
    const listener = vi.fn();
    window.addEventListener(ASK_DEEPSPACE_EVENT, listener);

    expect(dispatchAskDeepSeek("  selected\r\ntext  ", "tooltip")).toBe(true);
    expect(listener).toHaveBeenCalledTimes(1);
    const event = listener.mock.calls[0][0] as CustomEvent;
    expect(event.detail).toEqual({ text: "selected\ntext", source: "tooltip" });

    window.removeEventListener(ASK_DEEPSPACE_EVENT, listener);
  });

  it("does not dispatch an empty selection", () => {
    const listener = vi.fn();
    window.addEventListener(ASK_DEEPSPACE_EVENT, listener);

    expect(dispatchAskDeepSeek("   ", "desktop-menu")).toBe(false);
    expect(listener).not.toHaveBeenCalled();

    window.removeEventListener(ASK_DEEPSPACE_EVENT, listener);
  });
});

describe("selectionIsInSurface", () => {
  it("accepts content inside the DeepSpace surface", () => {
    const surface = document.createElement("div");
    surface.setAttribute("data-deepspace-selection-surface", "");
    const paragraph = document.createElement("p");
    paragraph.textContent = "message";
    surface.append(paragraph);
    document.body.append(surface);

    expect(selectionIsInSurface(paragraph)).toBe(true);
    surface.remove();
  });

  it("rejects controls, editable fields, and content outside the surface", () => {
    const surface = document.createElement("div");
    surface.setAttribute("data-deepspace-selection-surface", "");
    const textarea = document.createElement("textarea");
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    surface.append(textarea, editable);
    const outside = document.createElement("p");
    document.body.append(surface, outside);

    expect(selectionIsInSurface(textarea)).toBe(false);
    expect(selectionIsInSurface(editable)).toBe(false);
    expect(selectionIsInSurface(outside)).toBe(false);
    expect(selectionIsInSurface(null)).toBe(false);

    surface.remove();
    outside.remove();
  });
});

describe("readAskSelection", () => {
  it("returns normalized text, the anchor element, and the viewport rectangle", () => {
    const paragraph = document.createElement("p");
    paragraph.textContent = "selected";
    document.body.append(paragraph);

    const snapshot = readAskSelection(
      fakeSelection({ text: "  selected\r\ntext  ", anchorNode: paragraph.firstChild }),
    );

    expect(snapshot?.text).toBe("selected\ntext");
    expect(snapshot?.anchorElement).toBe(paragraph);
    expect(snapshot?.rect).toEqual({
      top: 10,
      left: 20,
      right: 120,
      bottom: 40,
      width: 100,
      height: 30,
    });

    paragraph.remove();
  });

  it("ignores collapsed or missing selections", () => {
    expect(readAskSelection(null)).toBeNull();
    expect(
      readAskSelection(fakeSelection({ text: "", anchorNode: null, rangeCount: 0 })),
    ).toBeNull();
  });

  it("keeps text when a zero-sized range cannot be positioned", () => {
    const paragraph = document.createElement("p");
    paragraph.textContent = "keyboard selection";
    document.body.append(paragraph);

    const snapshot = readAskSelection(
      fakeSelection({
        text: "keyboard selection",
        anchorNode: paragraph.firstChild,
        rect: { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 },
      }),
    );

    expect(snapshot?.text).toBe("keyboard selection");
    expect(snapshot?.rect).toBeNull();

    paragraph.remove();
  });
});

describe("composer limit", () => {
  it("matches the composer's own 4,000 character ceiling", () => {
    expect(COMPOSER_MAX_LENGTH).toBe(4_000);
  });
});

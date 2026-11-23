/**
 * Selection-to-composer helpers for the DeepSpace "Ask DeepSeek" action.
 *
 * The same helpers serve the floating tooltip and the native Electron menu,
 * so both paths normalize and insert selected text identically.
 */

export const ASK_DEEPSPACE_EVENT = "deepspace:ask-selection";
export const COMPOSER_MAX_LENGTH = 4_000;
export const MAX_SELECTION_LENGTH = 12_000;
export const DEEPSPACE_SELECTION_SURFACE = "[data-deepspace-selection-surface]";

const SELECTION_IGNORE_SELECTOR =
  "input, textarea, select, [contenteditable='true'], [data-deepspace-ask-ignore]";

export interface AskDeepSeekDetail {
  text: string;
  source: string;
}

export interface SelectionSnapshot {
  text: string;
  anchorElement: Element | null;
  rect: {
    top: number;
    left: number;
    right: number;
    bottom: number;
    width: number;
    height: number;
  } | null;
}

export interface InsertSelectionOptions {
  start?: number | null;
  end?: number | null;
  maxLength?: number;
}

export interface InsertSelectionResult {
  query: string;
  cursor: number;
  inserted: boolean;
}

/**
 * Trim and newline-normalize text a user selected.
 *
 * @param raw Value from `Selection.toString()` or the native menu payload.
 * @param maxLength Largest number of characters to keep.
 * @returns Normalized text, or an empty string when there is nothing to ask.
 */
export function normalizeSelectionText(raw: unknown, maxLength = MAX_SELECTION_LENGTH): string {
  if (typeof raw !== "string") return "";
  const normalized = raw
    .replace(/\r\n?/g, "\n")
    .replace(/\u0000/g, "")
    .trim();
  if (!normalized || maxLength <= 0) return "";
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, Math.max(0, maxLength - 1)).trimEnd()}…`;
}

function quoteBlock(selection: string, budget: number): string {
  if (budget <= 0) return "";
  const lines = selection.split("\n");
  const rendered: string[] = [];
  let used = 0;

  for (let index = 0; index < lines.length; index += 1) {
    const separatorCost = index === 0 ? 0 : 1;
    const markerCost = 2;
    const remaining = budget - used - separatorCost - markerCost;
    if (remaining <= 0) break;

    const line = lines[index];
    if (line.length > remaining) {
      const keep = Math.max(0, remaining - 1);
      const shortened = `${line.slice(0, keep)}…`;
      rendered.push(`> ${shortened}`);
      break;
    }

    rendered.push(`> ${line}`);
    used += separatorCost + markerCost + line.length;
  }

  return rendered.join("\n");
}

/**
 * Insert a selected passage into the composer as a block quote.
 *
 * Existing text is preserved. When the textarea has focus and a selection,
 * the passage replaces that range; otherwise it is inserted at the caret or
 * appended. The result never exceeds the composer's own character limit.
 *
 * @param query Current composer text.
 * @param selection Selected passage.
 * @param options Caret range and maximum composer length.
 * @returns The next composer text and caret position.
 */
export function insertSelectionIntoQuery(
  query: string,
  selection: unknown,
  options: InsertSelectionOptions = {},
): InsertSelectionResult {
  const maxLength = options.maxLength ?? COMPOSER_MAX_LENGTH;
  const source = typeof query === "string" ? query : "";
  const boundedSource = source.slice(0, Math.max(0, maxLength));
  const normalized = normalizeSelectionText(selection);

  if (!normalized || maxLength <= 0) {
    return {
      query: boundedSource,
      cursor: boundedSource.length,
      inserted: false,
    };
  }

  const startValue =
    typeof options.start === "number" && Number.isFinite(options.start)
      ? options.start
      : boundedSource.length;
  const endValue =
    typeof options.end === "number" && Number.isFinite(options.end) ? options.end : startValue;
  const start = Math.max(0, Math.min(startValue, boundedSource.length));
  const end = Math.max(start, Math.min(endValue, boundedSource.length));

  const before = boundedSource.slice(0, start);
  const after = boundedSource.slice(end);
  const leading = before.length > 0 && !before.endsWith("\n") ? "\n\n" : "";
  const trailing = after.length > 0 ? (after.startsWith("\n") ? "" : "\n\n") : "\n\n";
  const room = maxLength - before.length - after.length - leading.length - trailing.length;
  const quote = quoteBlock(normalized, room);

  if (!quote) {
    return { query: boundedSource, cursor: start, inserted: false };
  }

  const insertedText = `${leading}${quote}${trailing}`;
  const next = `${before}${insertedText}${after}`;
  const bounded = next.slice(0, maxLength);
  return {
    query: bounded,
    cursor: Math.min(before.length + insertedText.length, bounded.length),
    inserted: true,
  };
}

function nodeElement(node: Node | null): Element | null {
  if (!node) return null;
  if (node.nodeType === Node.ELEMENT_NODE) return node as Element;
  return node.parentElement;
}

/**
 * Read a DOM selection into the data the tooltip needs.
 *
 * @param selection Browser selection, or null.
 * @returns The normalized text, anchor element, and viewport rectangle.
 */
export function readAskSelection(selection: Selection | null): SelectionSnapshot | null {
  if (!selection || selection.rangeCount === 0) return null;
  const text = normalizeSelectionText(selection.toString());
  if (!text) return null;

  let rect: SelectionSnapshot["rect"] = null;
  try {
    const range = selection.getRangeAt(0);
    const raw = range.getBoundingClientRect();
    if (raw && (raw.width > 0 || raw.height > 0)) {
      rect = {
        top: raw.top,
        left: raw.left,
        right: raw.right,
        bottom: raw.bottom,
        width: raw.width,
        height: raw.height,
      };
    }
  } catch {
    rect = null;
  }

  return {
    text,
    anchorElement: nodeElement(selection.anchorNode),
    rect,
  };
}

/**
 * Decide whether a selection belongs to DeepSpace content we may quote.
 *
 * @param element The selection anchor or focus element.
 * @param surfaceSelector The DeepSpace surface selector.
 * @returns True when the element is inside the surface and not in a control.
 */
export function selectionIsInSurface(
  element: Element | null,
  surfaceSelector = DEEPSPACE_SELECTION_SURFACE,
): boolean {
  if (!element || typeof element.closest !== "function") return false;
  if (element.closest(SELECTION_IGNORE_SELECTOR)) return false;
  return Boolean(element.closest(surfaceSelector));
}

/**
 * Send selected text to every mounted DeepSpace composer.
 *
 * @param text Selected passage.
 * @param source Which surface triggered the action.
 * @returns True when a non-empty event was dispatched.
 */
export function dispatchAskDeepSeek(text: unknown, source = "tooltip"): boolean {
  if (typeof window === "undefined") return false;
  const normalized = normalizeSelectionText(text);
  if (!normalized) return false;
  window.dispatchEvent(
    new CustomEvent<AskDeepSeekDetail>(ASK_DEEPSPACE_EVENT, {
      detail: { text: normalized, source },
    }),
  );
  return true;
}

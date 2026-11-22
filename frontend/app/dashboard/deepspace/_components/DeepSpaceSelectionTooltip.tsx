"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Sparkles } from "lucide-react";

import {
  DEEPSPACE_SELECTION_SURFACE,
  dispatchAskDeepSeek,
  readAskSelection,
  selectionIsInSurface,
  type SelectionSnapshot,
} from "../_lib/ask-selection";

const SELECTION_REFRESH_DELAY_MS = 60;
const TOOLTIP_WIDTH = 176;
const TOOLTIP_HEIGHT = 36;
const VIEWPORT_MARGIN = 8;
const SELECTION_GAP = 8;

/**
 * Floating "Ask DeepSeek" button shown after the user selects DeepSpace text.
 *
 * The component only observes selections inside `data-deepspace-selection-surface`
 * and ignores inputs, textareas, and editable fields. It never submits a turn;
 * clicking dispatches the shared composer event.
 */
export default function DeepSpaceSelectionTooltip() {
  const [snapshot, setSnapshot] = useState<SelectionSnapshot | null>(null);

  const clear = useCallback(() => setSnapshot(null), []);

  const refresh = useCallback(() => {
    if (typeof window === "undefined") return;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) {
      clear();
      return;
    }

    const next = readAskSelection(selection);
    if (
      !next ||
      !next.rect ||
      !selectionIsInSurface(next.anchorElement, DEEPSPACE_SELECTION_SURFACE) ||
      !selectionIsInSurface(
        selection.focusNode
          ? selection.focusNode.nodeType === Node.ELEMENT_NODE
            ? (selection.focusNode as Element)
            : selection.focusNode.parentElement
          : null,
        DEEPSPACE_SELECTION_SURFACE,
      )
    ) {
      clear();
      return;
    }

    setSnapshot(next);
  }, [clear]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const schedule = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(refresh, SELECTION_REFRESH_DELAY_MS);
    };
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest("[data-deepspace-ask-tooltip]")) {
        return;
      }
      clear();
    };
    const handleScroll = () => clear();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") clear();
    };

    window.addEventListener("mouseup", schedule);
    window.addEventListener("keyup", schedule);
    document.addEventListener("selectionchange", schedule);
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("scroll", handleScroll, true);
    window.addEventListener("resize", handleScroll);
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      if (timer) clearTimeout(timer);
      window.removeEventListener("mouseup", schedule);
      window.removeEventListener("keyup", schedule);
      document.removeEventListener("selectionchange", schedule);
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("scroll", handleScroll, true);
      window.removeEventListener("resize", handleScroll);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [clear, refresh]);

  useEffect(() => {
    const bridge = window.electron?.contextMenu;
    if (!bridge?.onAskDeepSeek) return;
    return bridge.onAskDeepSeek((text) => {
      dispatchAskDeepSeek(text, "desktop-menu");
    });
  }, []);

  if (!snapshot || !snapshot.rect) return null;

  const viewportWidth = window.innerWidth || 1024;
  const viewportHeight = window.innerHeight || 768;
  const rect = snapshot.rect;
  const center = rect.left + rect.width / 2;
  const left = Math.min(
    Math.max(center, TOOLTIP_WIDTH / 2 + VIEWPORT_MARGIN),
    viewportWidth - TOOLTIP_WIDTH / 2 - VIEWPORT_MARGIN,
  );
  const placeBelow = rect.top < TOOLTIP_HEIGHT + VIEWPORT_MARGIN * 2;
  const top = placeBelow
    ? Math.min(rect.bottom + SELECTION_GAP, viewportHeight - TOOLTIP_HEIGHT - VIEWPORT_MARGIN)
    : Math.max(rect.top - SELECTION_GAP, TOOLTIP_HEIGHT + VIEWPORT_MARGIN);

  return createPortal(
    <div
      data-deepspace-ask-tooltip=""
      style={{
        left: `${left}px`,
        top: `${top}px`,
        transform: placeBelow ? "translate(-50%, 0)" : "translate(-50%, -100%)",
      }}
      className="fixed z-[1200]"
    >
      <button
        type="button"
        aria-label="Ask DeepSeek about the selected text"
        className="border-primary/35 bg-surface-0/95 text-foreground hover:bg-surface-1 inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[11px] font-semibold whitespace-nowrap shadow-xl backdrop-blur-md transition-colors"
        onPointerDown={(event) => event.preventDefault()}
        onClick={() => {
          dispatchAskDeepSeek(snapshot.text, "tooltip");
          // The passage is now in the composer. Drop the page selection so a
          // delayed selection refresh cannot bring the button back.
          window.getSelection()?.removeAllRanges();
          clear();
        }}
      >
        <Sparkles size={13} aria-hidden="true" />
        Ask DeepSeek
      </button>
    </div>,
    document.body,
  );
}

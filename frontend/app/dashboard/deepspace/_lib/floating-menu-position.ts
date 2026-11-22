export interface FloatingMenuPosition {
  top: number;
  left: number;
  maxHeight: number;
  maxWidth: number;
}

/** Positions a menu against the viewport so clipped drawers cannot hide it. */
export function positionFloatingMenu(
  anchor: DOMRect,
  menu: DOMRect,
  viewport: { width: number; height: number },
  preferredSide: "top" | "bottom",
): FloatingMenuPosition {
  const margin = 8;
  const gap = 4;
  const maxWidth = Math.max(0, viewport.width - margin * 2);
  const width = Math.min(menu.width, maxWidth);
  const height = Math.min(menu.height, Math.max(0, viewport.height - margin * 2));
  const topSpace = Math.max(0, anchor.top - gap - margin);
  const bottomSpace = Math.max(0, viewport.height - anchor.bottom - gap - margin);
  const side =
    preferredSide === "top"
      ? topSpace >= height || topSpace >= bottomSpace
        ? "top"
        : "bottom"
      : bottomSpace >= height || bottomSpace >= topSpace
        ? "bottom"
        : "top";
  const availableHeight = side === "top" ? topSpace : bottomSpace;
  const maxHeight = Math.min(height, availableHeight);
  const requestedLeft = anchor.right - width;
  const left = Math.max(margin, Math.min(requestedLeft, viewport.width - width - margin));
  const requestedTop = side === "top" ? anchor.top - gap - maxHeight : anchor.bottom + gap;
  const top = Math.max(margin, Math.min(requestedTop, viewport.height - maxHeight - margin));

  return { top, left, maxHeight, maxWidth };
}

"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";

export interface ContextMenuItem {
  id: string;
  label: string;
  icon?: React.ReactNode;
  shortcut?: string;
  disabled?: boolean;
  danger?: boolean;
  onClick?: () => void;
  submenu?: ContextMenuItem[];
}

export interface ContextMenuProps {
  items: ContextMenuItem[];
  x: number;
  y: number;
  onClose: () => void;
}

export function ContextMenu({ items, x, y, onClose }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ x, y });

  useEffect(() => {
    if (!ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    const maxX = window.innerWidth - rect.width - 8;
    const maxY = window.innerHeight - rect.height - 8;
    setPosition({
      x: Math.min(x, maxX),
      y: Math.min(y, maxY),
    });
  }, [x, y]);

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKey);
    };
  }, [onClose]);

  const handleItemClick = useCallback(
    (item: ContextMenuItem) => {
      if (item.disabled) return;
      item.onClick?.();
      if (!item.submenu) onClose();
    },
    [onClose]
  );

  return (
    <div
      ref={ref}
      className="fixed z-[9999] min-w-[180px] rounded-lg border border-[var(--border)] bg-[var(--bg-elevated)] py-1 shadow-xl backdrop-blur-sm"
      style={{ left: position.x, top: position.y }}
      role="menu"
    >
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="menuitem"
          disabled={item.disabled}
          onClick={() => handleItemClick(item)}
          className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm transition-colors ${
            item.disabled
              ? "cursor-not-allowed opacity-40"
              : item.danger
              ? "text-red-400 hover:bg-red-500/10"
              : "text-[var(--text-primary)] hover:bg-[var(--bg-hover)]"
          }`}
        >
          {item.icon && <span className="w-4 text-center">{item.icon}</span>}
          <span className="flex-1">{item.label}</span>
          {item.shortcut && (
            <span className="text-xs text-[var(--text-tertiary)]">{item.shortcut}</span>
          )}
          {item.submenu && <span className="text-xs">▸</span>}
        </button>
      ))}
    </div>
  );
}

export function useContextMenu() {
  const [menu, setMenu] = useState<{
    items: ContextMenuItem[];
    x: number;
    y: number;
  } | null>(null);

  const open = useCallback((items: ContextMenuItem[], x: number, y: number) => {
    setMenu({ items, x, y });
  }, []);

  const close = useCallback(() => setMenu(null), []);

  return { menu, open, close };
}

export default ContextMenu;

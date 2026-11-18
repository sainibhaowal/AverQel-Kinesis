"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { createPortal } from "react-dom";
import { Copy, RotateCw, Edit3, Trash2, GitBranch, FileText, ChevronRight, X, MessageSquare, Download, Eye, AlertTriangle } from "lucide-react";

import { SlotOutlet } from "@/app/dashboard/deepspace/_lib/module-registry";

interface ContextMenuItem {
  label: string;
  icon?: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
  shortcut?: string;
  subItems?: ContextMenuItem[];
}

interface DeepSpaceContextMenuProps {
  isOpen: boolean;
  onClose: () => void;
  items: ContextMenuItem[];
  position: { x: number; y: number };
  anchorRef?: React.RefObject<HTMLElement | null>;
}

function ContextMenuItemComponent({
  item,
  onClose,
  depth = 0,
}: {
  item: ContextMenuItem;
  onClose: () => void;
  depth?: number;
}) {
  const [subOpen, setSubOpen] = useState(false);
  const subMenuRef = useRef<HTMLDivElement>(null);
  const itemRef = useRef<HTMLButtonElement>(null);

  const handleClick = () => {
    if (item.disabled) return;
    if (item.subItems && item.subItems.length > 0) {
      setSubOpen(!subOpen);
      return;
    }
    item.onClick();
    onClose();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (item.disabled) return;

    switch (e.key) {
      case "Enter":
      case " ":
        e.preventDefault();
        handleClick();
        break;
      case "ArrowRight":
        if (item.subItems && item.subItems.length > 0) {
          e.preventDefault();
          setSubOpen(true);
        }
        break;
      case "ArrowLeft":
        if (depth > 0) {
          e.preventDefault();
          setSubOpen(false);
        }
        break;
      case "Escape":
        if (subOpen) {
          e.preventDefault();
          setSubOpen(false);
        } else {
          onClose();
        }
        break;
    }
  };

  return (
    <div className="relative">
      <button
        ref={itemRef}
        type="button"
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        disabled={item.disabled}
        className={`w-full flex items-center gap-2 px-3 py-1.5 text-left text-[12px] transition-colors rounded-md ${
          item.disabled
            ? "opacity-40 cursor-not-allowed"
            : item.danger
            ? "text-red-300 hover:bg-red-500/10 dark:hover:bg-red-500/10"
            : "text-foreground hover:bg-slate-100/50 dark:hover:bg-slate-800/50"
        }`}
        tabIndex={depth === 0 ? 0 : -1}
      >
        {item.icon && <span className="shrink-0 h-4 w-4">{item.icon}</span>}
        <span className="flex-1 truncate">{item.label}</span>
        {item.shortcut && (
          <kbd className="shrink-0 px-1.5 py-0.5 rounded text-[9px] font-mono border border-slate-300/50 bg-slate-100/50 text-muted-foreground dark:border-slate-600/50 dark:bg-slate-800/50">
            {item.shortcut}
          </kbd>
        )}
        {item.subItems && item.subItems.length > 0 && (
          <ChevronRight
            size={12}
            className={`shrink-0 text-muted-foreground/60 transition-transform ${subOpen ? "rotate-90" : ""}`}
          />
        )}
      </button>

      {subOpen && item.subItems && (
        <div
          ref={subMenuRef}
          className="deepspace-context-submenu absolute left-full top-0 min-w-[180px] rounded-md border border-slate-300/50 bg-white/95 shadow-xl backdrop-blur-md py-1 dark:border-cyan-300/20 dark:bg-slate-900/95"
          role="menu"
        >
          {item.subItems.map((subItem, index) => (
            <ContextMenuItemComponent
              key={`${item.label}-${index}`}
              item={subItem}
              onClose={onClose}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function DeepSpaceContextMenu({
  isOpen,
  onClose,
  items,
  position,
  anchorRef,
}: DeepSpaceContextMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [adjustedPosition, setAdjustedPosition] = useState(position);

  // Adjust position to stay within viewport
  useEffect(() => {
    if (!isOpen || !menuRef.current) return;

    const menu = menuRef.current;
    const menuRect = menu.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    let left = position.x;
    let top = position.y;

    // Adjust horizontal
    if (left + menuRect.width > viewportWidth - 8) {
      left = viewportWidth - menuRect.width - 8;
    }
    if (left < 8) left = 8;

    // Adjust vertical
    if (top + menuRect.height > viewportHeight - 8) {
      top = viewportHeight - menuRect.height - 8;
    }
    if (top < 8) top = 8;

    setAdjustedPosition({ x: left, y: top });
  }, [isOpen, position]);

  // Handle outside click
  useEffect(() => {
    if (!isOpen) return;

    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (menuRef.current?.contains(target)) return;
      if (anchorRef?.current?.contains(target)) return;
      onClose();
    };

    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };

    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleEscape);

    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [isOpen, onClose, anchorRef]);

  // Focus first item when open
  useEffect(() => {
    if (isOpen) {
      const firstButton = menuRef.current?.querySelector("button") as HTMLElement;
      firstButton?.focus();
    }
  }, [isOpen]);

  if (!isOpen || items.length === 0) return null;

  const menuContent = (
    <div
      ref={menuRef}
      className="deepspace-context-menu fixed z-[1000] min-w-[180px] max-w-[280px] rounded-xl border border-slate-300/50 bg-white/95 shadow-xl backdrop-blur-md py-1 dark:border-cyan-300/20 dark:bg-slate-900/95"
      style={{
        left: adjustedPosition.x,
        top: adjustedPosition.y,
      }}
      role="menu"
      aria-label="Context menu"
    >
      {items.map((item, index) => (
        <ContextMenuItemComponent
          key={`${item.label}-${index}`}
          item={item}
          onClose={onClose}
        />
      ))}
      {/* Module-contributed actions, rendered after the built-in items. */}
      <SlotOutlet slot="context-menu" />
    </div>
  );

  return createPortal(menuContent, document.body);
}

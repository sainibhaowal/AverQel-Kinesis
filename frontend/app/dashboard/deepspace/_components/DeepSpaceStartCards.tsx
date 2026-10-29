"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, FolderTree, Globe, SquareTerminal } from "lucide-react";

import type { TerminalShell } from "../_lib/workspace-panel-api";

/** The shells a new terminal session can run under. */
const SHELLS: TerminalShell[] = ["bash", "pwsh", "cmd"];

export interface DeepSpaceStartCardsProps {
  /** Whether the cards can open. A conversation without a workspace cannot. */
  disabled?: boolean;
  /** Called when the user opens the workspace files. */
  onOpenFiles: () => void;
  /** Called when the user opens a terminal under one shell. */
  onOpenTerminal: (shell: TerminalShell) => void;
  /** Called when the user opens the in-app browser. */
  onOpenBrowser: () => void;
}

/**
 * The three ways into a conversation's own tools.
 *
 * A conversation starts empty, so the panel's files, terminal, and browser
 * are one click away: the user picks the surface they want to work in, and
 * the side panel opens on it.
 */
export default function DeepSpaceStartCards({
  disabled = false,
  onOpenFiles,
  onOpenTerminal,
  onOpenBrowser,
}: DeepSpaceStartCardsProps) {
  const [shell, setShell] = useState<TerminalShell>("bash");
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const cardClass =
    "theme-panel-muted text-foreground flex w-full items-center gap-3 rounded-2xl border px-4 py-3.5 text-left transition-colors";

  return (
    <section aria-label="Start" className="w-full">
      <h2 className="text-muted-foreground mb-3 text-[11px] font-semibold tracking-[0.08em] uppercase">
        Start
      </h2>
      <div className="grid w-full grid-cols-1 gap-3">
        <button
          type="button"
          onClick={onOpenFiles}
          disabled={disabled}
          className={`${cardClass} hover:border-primary/25 disabled:cursor-not-allowed disabled:opacity-50`}
        >
          <FolderTree size={18} className="text-amber-500 shrink-0" />
          <span className="min-w-0 flex-1">
            <span className="block text-[13px] font-semibold">Workspace files</span>
            <span className="text-muted-foreground block truncate text-[11px]">
              Browse files in this session&rsquo;s workspace
            </span>
          </span>
        </button>

        <div
          className={`${cardClass} hover:border-primary/25 ${disabled ? "opacity-50" : ""}`}
          data-testid="deepspace-start-terminal"
        >
          <SquareTerminal size={18} className="text-muted-foreground shrink-0" />
          <button
            type="button"
            onClick={() => onOpenTerminal(shell)}
            disabled={disabled}
            className="min-w-0 flex-1 text-left disabled:cursor-not-allowed"
          >
            <span className="block text-[13px] font-semibold">New terminal</span>
            <span className="text-muted-foreground block truncate text-[11px]">
              Run commands in the Session workspace
            </span>
          </button>
          <div ref={menuRef} className="relative shrink-0">
            <button
              type="button"
              aria-label="Choose the terminal shell"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              disabled={disabled}
              onClick={() => setMenuOpen((open) => !open)}
              className="text-muted-foreground hover:bg-surface-1 hover:text-foreground rounded-md border border-transparent p-1.5 transition disabled:cursor-not-allowed"
            >
              <ChevronDown size={15} />
            </button>
            {menuOpen ? (
              <div
                role="menu"
                aria-label="Terminal shell"
                className="border-border-subtle bg-surface-1 absolute right-0 bottom-full z-20 mb-1 min-w-[7rem] overflow-hidden rounded-lg border py-1 shadow-lg"
              >
                {SHELLS.map((entry) => (
                  <button
                    key={entry}
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setShell(entry);
                      setMenuOpen(false);
                      onOpenTerminal(entry);
                    }}
                    className="text-foreground hover:bg-surface-2 block w-full px-3 py-1.5 text-left text-[11px] font-semibold"
                  >
                    {entry}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>

        <button
          type="button"
          onClick={onOpenBrowser}
          disabled={disabled}
          className={`${cardClass} hover:border-primary/25 disabled:cursor-not-allowed disabled:opacity-50`}
        >
          <Globe size={18} className="text-muted-foreground shrink-0" />
          <span className="min-w-0 flex-1">
            <span className="block text-[13px] font-semibold">Browser</span>
            <span className="text-muted-foreground block truncate text-[11px]">Browse web pages</span>
          </span>
        </button>
      </div>
    </section>
  );
}

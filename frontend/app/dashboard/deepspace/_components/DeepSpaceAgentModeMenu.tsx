"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Sparkles } from "lucide-react";

import type { AgentMode } from "../_lib/agent-mode-api";

export interface DeepSpaceAgentModeMenuProps {
  /** The modes the deployment offers. */
  modes: AgentMode[];
  /** The mode the conversation runs in. */
  mode: string;
  /** The default mode configured for new conversations. */
  defaultMode?: string;
  /** Called when the user picks another mode. */
  onModeChange: (mode: string) => void;
  /** Whether the menu can be opened. */
  disabled?: boolean;
  /** The classes the trigger button uses. */
  triggerClassName?: string;
  onMouseEnter?: (event: React.MouseEvent<HTMLButtonElement>) => void;
  onMouseLeave?: (event: React.MouseEvent<HTMLButtonElement>) => void;
  onFocus?: (event: React.FocusEvent<HTMLButtonElement>) => void;
  onBlur?: (event: React.FocusEvent<HTMLButtonElement>) => void;
  "aria-describedby"?: string;
}

/**
 * The agent mode menu.
 *
 * A mode says how the agent works: which tools it may call, whether it may
 * write a program that calls them, and whether a new conversation starts in
 * plan mode. The menu shows every mode the deployment offers, with its own
 * explanation, and the mode in force is marked.
 */
export default function DeepSpaceAgentModeMenu({
  modes,
  mode,
  defaultMode,
  onModeChange,
  disabled = false,
  triggerClassName,
  onMouseEnter,
  onMouseLeave,
  onFocus,
  onBlur,
  "aria-describedby": ariaDescribedBy,
}: DeepSpaceAgentModeMenuProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const current = modes.find((entry) => entry.id === mode);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative flex-shrink-0">
      <button
        type="button"
        aria-label="Agent mode"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-describedby={ariaDescribedBy}
        disabled={disabled || modes.length === 0}
        onMouseEnter={onMouseEnter}
        onMouseLeave={onMouseLeave}
        onFocus={onFocus}
        onBlur={onBlur}
        onClick={() => setOpen((value) => !value)}
        className={
          triggerClassName ??
          "border-glass-border bg-surface-2 text-foreground/60 hover:text-foreground flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold transition-all hover:bg-white/5 active:scale-95"
        }
      >
        <Sparkles size={11} />
        <span className="max-w-[10rem] truncate">{current?.name ?? "Agent mode"}</span>
        <ChevronDown size={11} className={`transition-transform duration-200 ${open ? "rotate-180" : ""}`} />
      </button>

      {open ? (
        <div
          role="menu"
          aria-label="Agent mode"
          className="border-glass-border bg-surface-2 absolute bottom-full left-0 z-[100] mb-2 max-h-[min(24rem,calc(100dvh-2rem))] w-[min(26rem,calc(100vw-2rem))] overflow-y-auto rounded-xl border p-1 shadow-xl"
        >
          {modes.map((entry) => {
            const selected = entry.id === mode;
            return (
              <button
                key={entry.id}
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                onClick={() => {
                  setOpen(false);
                  onModeChange(entry.id);
                }}
                className="hover:bg-white/[0.06] flex w-full items-start gap-2 rounded-lg px-2.5 py-2 text-left transition-all"
              >
                <span className="mt-0.5 w-3 shrink-0">
                  {selected ? <Check size={12} className="text-emerald-400" /> : null}
                </span>
                <span className="flex min-w-0 flex-col">
                  <span className="text-foreground/90 flex items-center gap-1.5 text-xs font-semibold">
                    <span>{entry.name}</span>
                    {defaultMode && entry.id === defaultMode ? (
                      <span className="border-primary/25 bg-primary/10 text-primary rounded border px-1.5 py-0.5 text-[9px] font-medium tracking-normal">
                        Default
                      </span>
                    ) : null}
                  </span>
                  <span className="text-muted-foreground mt-0.5 text-[10px] leading-4">
                    {entry.description}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

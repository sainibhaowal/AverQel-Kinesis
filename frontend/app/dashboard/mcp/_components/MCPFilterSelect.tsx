"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";

export interface MCPFilterOption {
  value: string;
  label: string;
}

interface MCPFilterSelectProps {
  label: string;
  value: string;
  options: MCPFilterOption[];
  onChange: (value: string) => void;
  className?: string;
}

export default function MCPFilterSelect({
  label,
  value,
  options,
  onChange,
  className = "",
}: MCPFilterSelectProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = options.find((option) => option.value === value);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div ref={rootRef} className={`relative shrink-0 ${className}`}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className={`inline-flex h-10 min-w-[8.5rem] items-center justify-between gap-3 rounded-xl border px-3.5 text-left text-xs font-bold transition-all shadow-2xs ${
          open
            ? "border-emerald-600 bg-emerald-50 text-emerald-950 dark:border-emerald-400 dark:bg-emerald-950/60 dark:text-emerald-200 shadow-xs"
            : "border-slate-300 bg-white text-slate-800 hover:border-slate-400 hover:bg-slate-50 dark:border-white/15 dark:bg-[#0e1612] dark:text-slate-200 dark:hover:border-white/30 dark:hover:bg-[#131d18]"
        }`}
      >
        <span className="truncate">{selected?.label || label}</span>
        <ChevronDown
          size={14}
          className={`shrink-0 text-slate-500 dark:text-white/60 transition-transform ${open ? "rotate-180" : ""}`}
          aria-hidden="true"
        />
      </button>

      {open && (
        <div
          role="listbox"
          aria-label={label}
          className="absolute top-[calc(100%+0.5rem)] right-0 z-[80] min-w-full overflow-hidden rounded-2xl border border-slate-200 bg-white p-1.5 shadow-xl dark:border-white/15 dark:bg-[#101713]/98 backdrop-blur-xl"
        >
          {options.map((option) => {
            const active = option.value === value;
            return (
              <button
                key={option.value}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => {
                  onChange(option.value);
                  setOpen(false);
                }}
                className={`flex w-full items-center justify-between gap-4 rounded-xl px-3 py-2 text-left text-xs transition-colors ${
                  active
                    ? "bg-emerald-600 text-white font-bold dark:bg-emerald-500/25 dark:text-emerald-100"
                    : "text-slate-800 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-white/[0.08] hover:text-slate-950 dark:hover:text-white font-semibold"
                }`}
              >
                <span className="whitespace-nowrap">{option.label}</span>
                {active ? <Check size={13} className="text-white dark:text-emerald-300 stroke-[2.5]" aria-hidden="true" /> : null}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

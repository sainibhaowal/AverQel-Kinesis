"use client";

import React from "react";

export interface MCPToggleSwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
  size?: "sm" | "md";
  title?: string;
}

export default function MCPToggleSwitch({
  checked,
  onChange,
  disabled = false,
  label,
  size = "md",
  title,
}: MCPToggleSwitchProps) {
  const isSm = size === "sm";

  return (
    <label
      title={title || (checked ? "Enabled (Click to disable)" : "Disabled (Click to enable)")}
      className={`relative inline-flex items-center gap-2 select-none ${
        disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer group"
      }`}
    >
      <input
        type="checkbox"
        className="sr-only"
        checked={checked}
        disabled={disabled}
        onChange={(e) => {
          if (!disabled) {
            onChange(e.target.checked);
          }
        }}
      />
      <div
        className={`relative inline-flex shrink-0 items-center rounded-full p-0.5 transition-colors duration-200 ease-in-out ${
          isSm ? "h-5 w-9" : "h-6 w-11"
        } ${
          checked
            ? "bg-emerald-500 shadow-[0_0_10px_rgba(16,185,129,0.35)] group-hover:bg-emerald-400"
            : "bg-slate-300 dark:bg-white/20 group-hover:bg-slate-400 dark:group-hover:bg-white/25"
        }`}
      >
        <span
          className={`pointer-events-none inline-block rounded-full bg-white shadow-sm transition-transform duration-200 ease-in-out ${
            isSm
              ? `h-4 w-4 ${checked ? "translate-x-4" : "translate-x-0"}`
              : `h-5 w-5 ${checked ? "translate-x-5" : "translate-x-0"}`
          }`}
        />
      </div>
      {label && (
        <span
          className={`font-semibold tracking-wide transition-colors ${
            isSm ? "text-[11px]" : "text-xs"
          } ${checked ? "text-emerald-700 dark:text-emerald-300" : "text-slate-600 dark:text-white/45"}`}
        >
          {label}
        </span>
      )}
    </label>
  );
}

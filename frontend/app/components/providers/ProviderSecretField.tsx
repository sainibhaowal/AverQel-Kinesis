"use client";

import { Eye, EyeOff, KeyRound } from "lucide-react";
import { useState } from "react";

interface ProviderSecretFieldProps {
  value: string;
  onChange: (value: string) => void;
  maskedSummary?: string | null;
  disabled?: boolean;
  label?: string;
  className?: string;
}

export default function ProviderSecretField({
  value,
  onChange,
  maskedSummary,
  disabled = false,
  label = "API Key / Token",
  className,
}: ProviderSecretFieldProps) {
  const [showSecret, setShowSecret] = useState(false);

  return (
    <div className="space-y-2">
      <label className="text-slate-800 dark:text-slate-100 block text-[10px] font-bold tracking-widest uppercase">
        {label}
      </label>
      <div className="relative">
        <KeyRound
          aria-hidden="true"
          className="text-muted-foreground/65 pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2"
        />
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          type={showSecret ? "text" : "password"}
          placeholder={maskedSummary || "Enter secret value"}
          disabled={disabled}
          className={`provider-api-key-input w-full rounded-lg border border-slate-400 bg-white py-0 pr-12 pl-10 text-sm text-slate-900 shadow-sm outline-none transition-colors placeholder:text-slate-500 focus:border-primary/70 focus:ring-2 focus:ring-primary/20 disabled:opacity-60 dark:border-slate-500 dark:bg-[#131b15] dark:text-slate-100 dark:placeholder:text-slate-400 ${className || ""}`}
        />
        <button
          type="button"
          onClick={() => setShowSecret((current) => !current)}
          className="text-muted-foreground hover:text-foreground focus-visible:ring-primary/50 absolute top-1/2 right-1.5 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md transition-colors hover:bg-black/5 focus-visible:ring-2 focus-visible:outline-none dark:hover:bg-white/5"
          aria-label="Toggle secret visibility"
        >
          {showSecret ? <EyeOff size={16} /> : <Eye size={16} />}
        </button>
      </div>
      <div className="text-muted-foreground flex items-center gap-2 text-xs">
        <span>
          {maskedSummary
            ? `Saved secret: ${maskedSummary}`
            : "The backend stores only encrypted secret material."}
        </span>
      </div>
    </div>
  );
}

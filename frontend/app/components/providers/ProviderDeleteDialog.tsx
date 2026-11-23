"use client";

import { AlertTriangle } from "lucide-react";
import { useEffect } from "react";

interface ProviderDeleteDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export default function ProviderDeleteDialog({
  open,
  title,
  body,
  confirmLabel,
  busy = false,
  onCancel,
  onConfirm,
}: ProviderDeleteDialogProps) {
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) {
        onCancel();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, busy, onCancel]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="provider-dialog-title"
    >
      {/* Translucent overlay behind the dialog - never a solid or white board */}
      <div
        className="animate-in fade-in fixed inset-0 bg-slate-950/45 backdrop-blur-[2px] transition-opacity duration-150 dark:bg-black/70"
        onClick={onCancel}
        aria-hidden="true"
      />
      {/* Small dialog box itself */}
      <div
        onClick={(e) => e.stopPropagation()}
        className="border-glass-border bg-surface-0 animate-in zoom-in-95 relative z-10 w-full max-w-sm space-y-5 rounded-3xl border p-6 shadow-[0_20px_50px_-12px_rgba(0,0,0,0.25)] transition-all duration-150 sm:p-7"
      >
        <div className="flex items-start gap-3.5">
          <div className="mt-0.5 shrink-0 rounded-xl border border-rose-500/25 bg-rose-500/10 p-2 text-rose-600 dark:text-rose-400">
            <AlertTriangle size={18} />
          </div>
          <div className="space-y-1">
            <h3
              id="provider-dialog-title"
              className="text-foreground text-base font-bold tracking-tight"
            >
              {title}
            </h3>
            <p className="text-muted-foreground text-xs leading-relaxed">{body}</p>
          </div>
        </div>
        <div className="border-glass-border flex items-center justify-end gap-2.5 border-t pt-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="theme-chip hover:text-foreground border-glass-border bg-surface-1 text-muted-foreground hover:bg-surface-2 cursor-pointer rounded-xl border px-4 py-2 text-xs font-semibold transition-all disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="cursor-pointer rounded-xl bg-rose-600 px-4 py-2 text-xs font-bold text-white shadow-md transition-all hover:bg-rose-500 disabled:opacity-60"
          >
            {busy ? "Working..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

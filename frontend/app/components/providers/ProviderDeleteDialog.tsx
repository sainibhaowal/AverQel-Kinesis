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
        className="fixed inset-0 bg-slate-950/45 dark:bg-black/70 backdrop-blur-[2px] transition-opacity animate-in fade-in duration-150"
        onClick={onCancel}
        aria-hidden="true"
      />
      {/* Small dialog box itself */}
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative z-10 w-full max-w-sm space-y-5 rounded-3xl border border-glass-border bg-surface-0 p-6 sm:p-7 shadow-[0_20px_50px_-12px_rgba(0,0,0,0.25)] transition-all animate-in zoom-in-95 duration-150"
      >
        <div className="flex items-start gap-3.5">
          <div className="mt-0.5 rounded-xl border border-rose-500/25 bg-rose-500/10 p-2 text-rose-600 dark:text-rose-400 shrink-0">
            <AlertTriangle size={18} />
          </div>
          <div className="space-y-1">
            <h3 id="provider-dialog-title" className="text-foreground text-base font-bold tracking-tight">
              {title}
            </h3>
            <p className="text-muted-foreground text-xs leading-relaxed">{body}</p>
          </div>
        </div>
        <div className="flex items-center justify-end gap-2.5 pt-2 border-t border-glass-border">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="theme-chip hover:text-foreground rounded-xl border border-glass-border bg-surface-1 px-4 py-2 text-xs font-semibold text-muted-foreground hover:bg-surface-2 transition-all disabled:opacity-50 cursor-pointer"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="rounded-xl bg-rose-600 px-4 py-2 text-xs font-bold text-white shadow-md hover:bg-rose-500 transition-all disabled:opacity-60 cursor-pointer"
          >
            {busy ? "Working..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import { ArrowRight, AlertTriangle, KeyRound, ExternalLink, X, ShieldAlert, Sparkles } from "lucide-react";
import ComposioLogo from "@/app/components/icons/ComposioLogo";
import type { MCPMarketplaceEntry, MCPConnection } from "@/lib/mcp-api";

export interface ComposioConnectWarningModalProps {
  isOpen: boolean;
  onClose: () => void;
  entry: MCPMarketplaceEntry | null;
  server?: MCPConnection | null;
  message?: string | null;
}

export default function ComposioConnectWarningModal({
  isOpen,
  onClose,
  entry,
  server,
  message,
}: ComposioConnectWarningModalProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen || !entry) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="composio-modal-title"
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/70 p-4 backdrop-blur-md transition-all animate-in fade-in duration-200"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="relative w-full max-w-lg overflow-hidden rounded-[1.75rem] border border-amber-500/30 bg-white p-6 shadow-[0_25px_50px_-12px_rgba(0,0,0,0.5),0_0_30px_rgba(245,158,11,0.15)] dark:border-amber-500/30 dark:bg-[#0c1411] dark:text-slate-100"
      >
        {/* Close Button */}
        <button
          type="button"
          onClick={onClose}
          aria-label="Close dialog"
          className="absolute top-4 right-4 flex h-8 w-8 items-center justify-center rounded-full text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-white/10 dark:hover:text-white"
        >
          <X className="h-4 w-4" />
        </button>

        {/* Top Badges */}
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-[10px] font-bold tracking-wider text-amber-600 uppercase dark:text-amber-400">
            <AlertTriangle className="h-3 w-3" />
            Composio Setup Required
          </span>
          <span className="inline-flex items-center gap-1 rounded-full border border-cyan-500/30 bg-cyan-500/10 px-2 py-0.5 text-[9px] font-bold tracking-wider text-cyan-600 uppercase dark:text-cyan-400">
            Cloud MCP Gateway
          </span>
        </div>

        {/* Header with visual integration banner */}
        <div className="mb-4 flex items-center gap-3">
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border border-glass-border bg-slate-50 shadow-sm dark:bg-white/5">
            <ComposioLogo className="h-6 w-6" />
          </div>
          <div>
            <h2 id="composio-modal-title" className="text-lg font-bold tracking-tight text-slate-900 dark:text-white">
              Connect {entry.name}
            </h2>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              1-Click cloud authorization via Composio Provider
            </p>
          </div>
        </div>

        {/* Body Message */}
        <p className="mb-4 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
          {message || (
            <>
              Connecting to <strong className="text-slate-900 dark:text-white">{entry.name}</strong> requires an active{" "}
              <strong>Composio Cloud MCP</strong> integration. Your Composio API key has not been configured yet, so 1-click
              cloud authorization could not be started.
            </>
          )}
        </p>

        {/* Status Checklist Card */}
        <div className="mb-6 space-y-2.5 rounded-2xl border border-slate-200/80 bg-slate-50/80 p-3.5 text-xs dark:border-white/10 dark:bg-white/5">
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-2 font-medium text-slate-700 dark:text-slate-300">
              <KeyRound className="h-3.5 w-3.5 text-amber-500" />
              Composio API Key
            </span>
            <span className="font-semibold text-amber-600 dark:text-amber-400">Not Configured</span>
          </div>
          <div className="flex items-center justify-between border-t border-slate-200/60 pt-2 dark:border-white/5">
            <span className="flex items-center gap-2 font-medium text-slate-700 dark:text-slate-300">
              <ShieldAlert className="h-3.5 w-3.5 text-cyan-500" />
              Target MCP Service
            </span>
            <span className="font-semibold text-slate-900 dark:text-white">{entry.name}</span>
          </div>
          <div className="flex items-center justify-between border-t border-slate-200/60 pt-2 dark:border-white/5">
            <span className="flex items-center gap-2 font-medium text-slate-700 dark:text-slate-300">
              <Sparkles className="h-3.5 w-3.5 text-emerald-500" />
              Required Action
            </span>
            <span className="font-medium text-slate-500 dark:text-slate-400">Save key in Provider Settings</span>
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:items-center sm:justify-end">
          {server?.id ? (
            <Link
              href={`/dashboard/mcp/inspector/${encodeURIComponent(server.id)}`}
              onClick={onClose}
              className="inline-flex items-center justify-center rounded-xl border border-slate-300 px-3.5 py-2 text-xs font-semibold text-slate-700 transition-colors hover:bg-slate-100 dark:border-white/15 dark:text-slate-200 dark:hover:bg-white/10"
            >
              Inspect Server
            </Link>
          ) : (
            <button
              type="button"
              onClick={onClose}
              className="inline-flex items-center justify-center rounded-xl border border-slate-300 px-3.5 py-2 text-xs font-semibold text-slate-700 transition-colors hover:bg-slate-100 dark:border-white/15 dark:text-slate-200 dark:hover:bg-white/10"
            >
              Cancel
            </button>
          )}

          <Link
            href="/dashboard/settings/providers?tab=mcp"
            onClick={onClose}
            className="inline-flex items-center justify-center gap-1.5 rounded-xl bg-gradient-to-r from-cyan-600 to-teal-600 px-4 py-2 text-xs font-bold text-white shadow-md transition-all hover:from-cyan-500 hover:to-teal-500 hover:shadow-cyan-500/25 active:scale-95 dark:from-cyan-500 dark:to-teal-500 dark:text-slate-950 dark:hover:from-cyan-400 dark:hover:to-teal-400"
          >
            <span>Configure Composio Key</span>
            <ArrowRight className="h-3.5 w-3.5" />
          </Link>
        </div>
      </div>
    </div>
  );
}

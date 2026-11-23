"use client";

import { useEffect, useState } from "react";
import {
  X,
  ExternalLink,
  CheckCircle2,
  AlertCircle,
  ShieldCheck,
  Zap,
  Globe2,
  FileCode2,
} from "lucide-react";
import Link from "next/link";

import { safeExternalUrl, type MCPConnection, type MCPMarketplaceEntry } from "@/lib/mcp-api";

import { MCPLogo } from "./MCPMarketplaceCard";
import MCPCommunityWarning from "./MCPCommunityWarning";
import MCPToolDescription from "./MCPToolDescription";

function transportLabel(value?: string | null): string {
  switch (value) {
    case "streamable_http":
      return "Remote HTTP";
    case "sse":
      return "Remote SSE";
    case "stdio":
      return "Local stdio";
    case "ssh":
      return "Remote SSH";
    default:
      return value ? value.replaceAll("_", " ") : "Remote";
  }
}

function riskLabel(value: string): string {
  return value === "external_message" ? "external message" : value;
}

function ExternalResource({ label, value }: { label: string; value?: string | null }) {
  const url = safeExternalUrl(value);
  if (!url) return null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700 underline decoration-emerald-500/30 underline-offset-4 transition-colors hover:text-emerald-800 dark:text-emerald-400 dark:hover:text-emerald-300"
    >
      {label}
      <ExternalLink className="h-3 w-3" aria-hidden="true" />
    </a>
  );
}

export interface MCPDetailDialogProps {
  entry: MCPMarketplaceEntry | null;
  connectedServer?: MCPConnection | null;
  onClose: () => void;
  onConnect: (entry: MCPMarketplaceEntry) => void;
  onReconnect?: (server: MCPConnection) => void;
  connecting?: boolean;
}

export default function MCPDetailDialog({
  entry,
  connectedServer,
  onClose,
  onConnect,
  onReconnect,
  connecting = false,
}: MCPDetailDialogProps) {
  useEffect(() => {
    if (!entry) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [entry, onClose]);

  if (!entry) return null;

  const tools = entry.tools || entry.tool_preview || [];
  const badges = entry.badges || {};
  const isCommunity = entry.publisher_type === "community" || badges.community;
  const isConnected = connectedServer?.status === "connected";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="mcp-dialog-title"
      onClick={onClose}
      className="animate-in fade-in fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-3 backdrop-blur-md duration-150 sm:p-5"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="animate-in zoom-in-95 relative flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-3xl border border-slate-200/90 bg-white text-slate-800 shadow-2xl duration-200 dark:border-white/15 dark:bg-[#111815] dark:text-white"
      >
        {/* Header Bar */}
        <div className="flex items-start justify-between gap-4 border-b border-slate-200/80 bg-slate-50/80 p-5 sm:p-6 dark:border-white/10 dark:bg-white/[0.02]">
          <div className="flex min-w-0 items-start gap-3.5">
            <MCPLogo entry={entry} size={48} />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h2
                  id="mcp-dialog-title"
                  className="truncate text-base font-bold text-slate-900 sm:text-lg dark:text-white"
                >
                  {entry.name}
                </h2>
                {entry.official && !isCommunity && (
                  <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold text-emerald-800 dark:text-emerald-300">
                    Official
                  </span>
                )}
                {entry.verified && (
                  <span className="rounded-full border border-teal-500/30 bg-teal-500/10 px-2 py-0.5 text-[10px] font-semibold text-teal-800 dark:text-teal-300">
                    Verified
                  </span>
                )}
                {isCommunity && (
                  <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold text-amber-800 dark:text-amber-300">
                    Community
                  </span>
                )}
              </div>
              <p className="mt-1 truncate text-xs text-slate-500 dark:text-white/50">
                by {entry.publisher || entry.author_name || "Community Publisher"}
              </p>
            </div>
          </div>

          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            className="shrink-0 rounded-xl p-2 text-slate-400 transition hover:bg-slate-200/80 hover:text-slate-800 dark:text-white/45 dark:hover:bg-white/10 dark:hover:text-white"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Scrollable Content Body */}
        <div className="space-y-5 divide-y divide-slate-200/70 overflow-y-auto p-5 sm:p-6 dark:divide-white/5">
          {/* Active Connection Banner */}
          {connectedServer && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-3.5 text-xs text-emerald-900 shadow-sm dark:text-emerald-200">
              <div className="flex items-center gap-2.5">
                <span className="relative flex h-2.5 w-2.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75"></span>
                  <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-emerald-500"></span>
                </span>
                <div>
                  <span className="font-semibold text-emerald-950 dark:text-emerald-100">
                    Connection Active
                  </span>
                  <span className="ml-1 text-[11px] text-emerald-800/80 dark:text-emerald-300/80">
                    ({connectedServer.status}) · Ready for DeepSpace
                  </span>
                </div>
              </div>
              <Link
                href={`/dashboard/mcp/inspector/${encodeURIComponent(connectedServer.id)}`}
                className="rounded-xl bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm transition hover:bg-emerald-500 dark:bg-emerald-500 dark:text-slate-950"
              >
                Inspect Tools →
              </Link>
            </div>
          )}

          {/* Quick Info & Primary Actions */}
          <div className="flex flex-wrap items-center justify-between gap-3 pt-2">
            <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
              <span className="rounded-lg border border-slate-200 bg-slate-100 px-2.5 py-1 font-medium text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-white/70">
                {transportLabel(entry.transport)}
              </span>
              <span className="rounded-lg border border-slate-200 bg-slate-100 px-2.5 py-1 font-medium text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-white/70">
                {entry.auth_type === "oauth" ? "OAuth 2.0" : entry.auth_type || "Anonymous"}
              </span>
              {entry.tool_count ? (
                <span className="flex items-center gap-1 rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-2.5 py-1 font-semibold text-emerald-800 dark:text-emerald-300">
                  <Zap className="h-3 w-3" />
                  {entry.tool_count} {entry.tool_count === 1 ? "tool" : "tools"}
                </span>
              ) : null}
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={(!entry.connectable && !connectedServer) || connecting}
                onClick={() => {
                  if (connectedServer && onReconnect) {
                    onReconnect(connectedServer);
                  } else {
                    onConnect(entry);
                  }
                }}
                className="inline-flex items-center justify-center gap-1.5 rounded-xl border border-teal-600/35 bg-teal-500/15 px-4 py-2 text-xs font-semibold text-teal-900 shadow-sm transition hover:bg-teal-500/25 disabled:cursor-not-allowed disabled:opacity-50 dark:text-teal-200"
              >
                {connecting ? (
                  "Connecting…"
                ) : connectedServer ? (
                  <>
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Reconnect
                  </>
                ) : entry.connectable ? (
                  <>
                    <ExternalLink className="h-3.5 w-3.5" />
                    Connect Connector
                  </>
                ) : (
                  "Setup pending"
                )}
              </button>
            </div>
          </div>

          {/* Warnings or Connectability reason */}
          {isCommunity && (
            <div className="pt-4">
              <MCPCommunityWarning />
            </div>
          )}

          {!entry.connectable && entry.connectability_reason && (
            <div className="pt-4">
              <div className="flex items-start gap-2.5 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
                <p>{entry.connectability_reason}</p>
              </div>
            </div>
          )}

          {/* Description */}
          <div className="space-y-2 pt-4">
            <h3 className="text-xs font-bold tracking-wider text-slate-500 uppercase dark:text-white/40">
              About Connector
            </h3>
            <p className="text-xs leading-relaxed whitespace-pre-wrap text-slate-700 sm:text-sm dark:text-white/75">
              {entry.description || "No detailed description is available."}
            </p>

            {/* Supported products & tags */}
            {entry.supported_products && entry.supported_products.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 pt-2">
                <span className="mr-1 text-[11px] text-slate-400 dark:text-white/40">
                  Products:
                </span>
                {entry.supported_products.map((p) => (
                  <span
                    key={p}
                    className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[10.5px] font-medium text-emerald-800 dark:text-emerald-300"
                  >
                    {p}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Tools & Capabilities */}
          <div className="space-y-3 pt-4">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-bold tracking-wider text-slate-500 uppercase dark:text-white/40">
                Reviewed Tools & Capabilities
              </h3>
              <span className="text-[11px] text-slate-500 dark:text-white/45">
                {tools.length} published
              </span>
            </div>

            {tools.length === 0 ? (
              <p className="py-2 text-xs text-slate-500 dark:text-white/45">
                This connector uses standard tool discovery upon live connection.
              </p>
            ) : (
              <div className="max-h-60 divide-y divide-slate-200/60 overflow-y-auto rounded-2xl border border-slate-200/80 bg-slate-50/50 p-2 dark:divide-white/5 dark:border-white/10 dark:bg-white/[0.02]">
                {tools.map((tool) => (
                  <div
                    key={tool.name}
                    className="flex flex-col gap-2 p-2.5 sm:flex-row sm:items-start sm:justify-between"
                  >
                    <MCPToolDescription tool={tool} />
                    <div className="flex shrink-0 flex-wrap gap-1">
                      {(tool.risk_labels?.length ? tool.risk_labels : ["read"]).map((risk) => (
                        <span
                          key={risk}
                          className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-medium text-slate-600 dark:border-white/10 dark:bg-white/5 dark:text-white/60"
                        >
                          {riskLabel(risk)}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* External Links */}
          <div className="space-y-2 pt-4">
            <h3 className="text-xs font-bold tracking-wider text-slate-500 uppercase dark:text-white/40">
              Resources & Documentation
            </h3>
            <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs">
              <ExternalResource
                label={entry.author_name ? `Author: ${entry.author_name}` : "Author website"}
                value={entry.author_website_url}
              />
              <ExternalResource label="Documentation" value={entry.docs_url} />
              <ExternalResource label="Support" value={entry.support_url} />
              <ExternalResource label="Privacy policy" value={entry.privacy_policy_url} />
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end border-t border-slate-200/80 bg-slate-50/80 p-4 dark:border-white/10 dark:bg-white/[0.02]">
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl border border-slate-300 px-4 py-2 text-xs font-medium text-slate-700 transition hover:bg-slate-100 hover:text-slate-900 dark:border-white/15 dark:text-white/70 dark:hover:bg-white/10 dark:hover:text-white"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Minus, Sparkles, Server } from "lucide-react";
import type { MCPConnection } from "@/lib/mcp-api";
import { MCPLogo } from "@/app/dashboard/mcp/_components/MCPMarketplaceCard";
import ComposioLogo from "@/app/components/icons/ComposioLogo";

export interface MCPInventoryItem {
  id: string;
  name: string;
  subtitle: string;
  badge: "Managed" | "Local" | "Hosted";
  isLocal: boolean;
  isManaged: boolean;
  statusBadge: React.ReactNode;
  icon?: React.ReactNode;
  rawServer?: MCPConnection;
  onRemove?: () => void;
}

interface MCPInventoryListProps {
  servers: MCPConnection[];
  composioConfigured: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onDeleteServer?: (server: MCPConnection) => void;
  onDeleteComposio?: () => void;
}

export default function MCPInventoryList({
  servers,
  composioConfigured,
  selectedId,
  onSelect,
  onDeleteServer,
  onDeleteComposio,
}: MCPInventoryListProps) {
  // Build the list of active MCP Provider families only (NO marketplace apps)
  const items: MCPInventoryItem[] = [];

  // 1. Composio Managed Cloud Provider
  items.push({
    id: "composio",
    name: "Composio Provider",
    subtitle: "composio · cloud mcp provider",
    badge: "Managed",
    isLocal: false,
    isManaged: true,
    statusBadge: (
      <span
        className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[9px] font-bold tracking-wider uppercase ${
          composioConfigured
            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 shadow-[0_0_8px_rgba(16,185,129,0.2)] dark:text-emerald-300"
            : "border-amber-600/60 bg-amber-500/15 text-amber-900 dark:text-amber-200"
        }`}
      >
        {composioConfigured && (
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
        )}
        {composioConfigured ? "Connected" : "Setup Required"}
      </span>
    ),
    icon: (
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-glass-border bg-surface-1/90 text-primary shadow-sm">
        <ComposioLogo className="h-5 w-5" />
      </div>
    ),
    onRemove: onDeleteComposio,
  });

  // 2. Custom Infrastructure Gateways / Runtimes (only if explicitly configured as custom providers, NOT marketplace apps)
  for (const server of servers) {
    // Only include servers that are dedicated custom gateways or local runtimes (not marketplace entries like Google Drive or Slack)
    const isCustomGateway =
      server.config?.source === "custom" ||
      server.name.toLowerCase().includes("gateway") ||
      server.name.toLowerCase().includes("runner");

    if (isCustomGateway) {
      const isConnected = server.status === "connected";
      const isNeedsAuth = server.status === "needs_auth";
      const isLocal = server.transport === "stdio";

      items.push({
        id: server.id,
        name: server.name,
        subtitle: `${server.transport.toUpperCase()} · ${isLocal ? "LOCAL RUNTIME" : "GATEWAY"}`,
        badge: isLocal ? "Local" : "Hosted",
        isLocal,
        isManaged: false,
        rawServer: server,
        onRemove: onDeleteServer ? () => onDeleteServer(server) : undefined,
        statusBadge: (
          <span
            className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[9px] font-bold tracking-wider uppercase ${
              isConnected
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 shadow-[0_0_8px_rgba(16,185,129,0.2)] dark:text-emerald-300"
                : isNeedsAuth
                  ? "border-amber-600/60 bg-amber-500/15 text-amber-900 dark:text-amber-200"
                  : "border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300"
            }`}
          >
            {isConnected && (
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
            )}
            {server.status.replace("_", " ")}
          </span>
        ),
        icon: (
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-glass-border bg-surface-1/90 text-primary shadow-sm">
            <Server className="h-5 w-5" />
          </div>
        ),
      });
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-transparent">
      {/* Category header matching LLM / Embedding / Web families */}
      <div className="border-glass-border mb-6 flex flex-col gap-1 border-b pb-6">
        <p className="text-muted-foreground font-mono text-[10px] tracking-[0.24em] uppercase">
          MCP Server Families
        </p>
        <div className="flex items-center justify-between">
          <h2 className="text-foreground text-lg font-bold">Inventory</h2>
          <div className="theme-pill opacity-70">{items.length} options</div>
        </div>
      </div>

      {/* Scrollable inventory list */}
      <div className="scrollbar-thin min-h-0 flex-1 space-y-4 overflow-x-hidden overflow-y-auto pr-1">
        {items.length === 0 ? (
          <div className="text-muted-foreground border-glass-border rounded-2xl border border-dashed p-8 text-center text-xs italic">
            No active connections.
          </div>
        ) : (
          items.map((item) => {
            const selected = item.id === selectedId;

            return (
              <div
                key={item.id}
                className={`group hover-yellow relative w-full rounded-[1.25rem] border transition-all duration-300 ${
                  selected
                    ? "border-primary/40 bg-surface-1 ring-primary/20 shadow-[0_4px_20px_-8px_hsl(var(--primary)/0.15)] ring-1"
                    : "border-glass-border bg-surface-0"
                }`}
              >
                <button
                  type="button"
                  onClick={() => onSelect(item.id)}
                  className="relative z-10 flex w-full flex-col space-y-3 rounded-[1.25rem] p-4 text-left"
                >
                  <div className="flex h-10 items-center justify-between gap-3 pr-10">
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center">
                      {item.icon || <Server className="h-5 w-5 text-primary" />}
                    </div>
                    <div className="flex flex-row items-center gap-2">
                      <span
                        className={`rounded-full border px-2 py-0.5 text-[8px] font-bold tracking-wider uppercase ${
                          item.isManaged
                            ? "border-primary/20 bg-primary/5 text-primary"
                            : item.isLocal
                              ? "border-emerald-500/20 bg-emerald-500/5 text-emerald-700 dark:text-emerald-300"
                              : "border-glass-border bg-muted text-muted-foreground"
                        }`}
                      >
                        {item.badge}
                      </span>
                      {selected && <CheckIcon />}
                    </div>
                  </div>

                  <div className="min-w-0">
                    <div
                      className={`truncate text-sm font-bold tracking-tight ${
                        selected ? "text-primary" : "text-foreground"
                      }`}
                    >
                      {item.name}
                    </div>
                    <div className="text-muted-foreground mt-0.5 truncate text-[11px] font-medium">
                      {item.subtitle}
                    </div>
                  </div>

                  <div className="flex items-center justify-between pt-1">
                    {item.statusBadge}
                  </div>
                </button>

                {item.onRemove ? (
                  <RemoveServerButton
                    serverName={item.name}
                    onRemove={item.onRemove}
                  />
                ) : null}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function CheckIcon() {
  return (
    <div className="bg-primary text-primary-foreground flex h-4 w-4 items-center justify-center rounded-full">
      <svg
        width="10"
        height="10"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <polyline points="20 6 9 17 4 12"></polyline>
      </svg>
    </div>
  );
}

interface RemoveServerButtonProps {
  serverName: string;
  onRemove: () => void;
}

interface TooltipPosition {
  top: number;
  left: number;
  above: boolean;
}

function RemoveServerButton({ serverName, onRemove }: RemoveServerButtonProps) {
  const [tooltipOpen, setTooltipOpen] = useState(false);
  const [tooltipPosition, setTooltipPosition] = useState<TooltipPosition | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const tooltipId = useId();

  useEffect(() => {
    if (!tooltipOpen) return;

    const updatePosition = () => {
      const button = buttonRef.current;
      if (!button) return;

      const rect = button.getBoundingClientRect();
      const halfWidth = 50;
      const left = Math.max(
        halfWidth + 8,
        Math.min(window.innerWidth - halfWidth - 8, rect.left + rect.width / 2),
      );
      const above = rect.top > 52;

      setTooltipPosition({
        top: above ? rect.top - 8 : rect.bottom + 8,
        left,
        above,
      });
    };

    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);

    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [tooltipOpen]);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          setTooltipOpen(false);
          onRemove();
        }}
        onPointerEnter={(event) => {
          if (event.pointerType === "mouse") setTooltipOpen(true);
        }}
        onPointerLeave={() => setTooltipOpen(false)}
        onFocus={() => setTooltipOpen(true)}
        onBlur={() => setTooltipOpen(false)}
        aria-label={`Remove ${serverName}`}
        aria-describedby={tooltipOpen ? tooltipId : undefined}
        className="border-glass-border bg-surface-2 text-foreground/80 absolute top-5 right-3 z-20 flex h-8 w-8 items-center justify-center rounded-full border transition-colors hover:border-red-500/45 hover:bg-red-500/12 hover:text-red-300 focus-visible:ring-2 focus-visible:ring-red-500/45 focus-visible:outline-none"
      >
        <Minus size={16} strokeWidth={2.5} aria-hidden="true" />
      </button>
      {tooltipOpen && tooltipPosition && typeof document !== "undefined"
        ? createPortal(
            <div
              id={tooltipId}
              role="tooltip"
              className="border-glass-border text-foreground pointer-events-none fixed z-[10000] w-max max-w-[calc(100vw-1rem)] rounded-lg border px-2.5 py-1.5 text-[11px] leading-tight font-semibold shadow-[0_12px_32px_rgba(0,0,0,0.45)]"
              style={{
                top: tooltipPosition.top,
                left: tooltipPosition.left,
                transform: `translate(-50%, ${tooltipPosition.above ? "-100%" : "0"})`,
                backgroundColor: "var(--background)",
              }}
            >
              Disconnect connection
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

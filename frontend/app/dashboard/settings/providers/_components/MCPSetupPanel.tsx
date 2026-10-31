"use client";

import { useState } from "react";
import Link from "next/link";
import {
  Zap,
  Sparkles,
  Key,
  KeyRound,
  Eye,
  EyeOff,
  CheckCircle2,
  AlertCircle,
  ExternalLink,
  Trash2,
  Loader2,
  Server,
  Layers,
  ArrowRight,
  ShieldCheck,
  Globe,
  Terminal,
  Activity,
  ArrowLeft,
  Lock,
  Cpu,
} from "lucide-react";
import toast from "react-hot-toast";

import {
  saveComposioSettings,
  deleteComposioSettings,
  createCustomMCPServer,
  toggleMCPServer,
  type MCPConnection,
} from "@/lib/mcp-api";
import { MCPLogo } from "@/app/dashboard/mcp/_components/MCPMarketplaceCard";
import MCPToggleSwitch from "@/app/dashboard/mcp/_components/MCPToggleSwitch";
import ComposioLogo from "@/app/components/icons/ComposioLogo";



interface MCPSetupPanelProps {
  selectedId: string | null;
  showCreateFlow: boolean;
  selectedCatalogType: string | null;
  composioSettings: { configured: boolean; masked_key: string | null };
  servers: MCPConnection[];
  isNarrowViewport: boolean;
  isInventoryExpanded: boolean;
  onOpenInventory?: () => void;
  onCancelCreate: () => void;
  onComposioUpdated: (settings: { configured: boolean; masked_key: string | null }) => void;
  onServerUpdated: (server: MCPConnection) => void;
  onDeleteServer?: (server: MCPConnection) => void;
  onServerCreated?: (server: MCPConnection) => void;
}

export default function MCPSetupPanel({
  selectedId,
  showCreateFlow,
  selectedCatalogType,
  composioSettings,
  servers,
  isNarrowViewport,
  isInventoryExpanded,
  onOpenInventory,
  onCancelCreate,
  onComposioUpdated,
  onServerUpdated,
  onDeleteServer,
  onServerCreated,
}: MCPSetupPanelProps) {
  // Composio API Key local edit state
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [showKeyInput, setShowKeyInput] = useState(!composioSettings.configured);
  const [savingKey, setSavingKey] = useState(false);
  const [deletingKey, setDeletingKey] = useState(false);
  const [showSecret, setShowSecret] = useState(false);

  // New MCP Server Form state
  const [newName, setNewName] = useState("");
  const [newServerUrl, setNewServerUrl] = useState("");
  const [newTransport, setNewTransport] = useState<"streamable_http" | "sse">("streamable_http");
  const [newCommand, setNewCommand] = useState("");
  const [newArgs, setNewArgs] = useState("");
  const [newAuthToken, setNewAuthToken] = useState("");
  const [creatingServer, setCreatingServer] = useState(false);

  // Live toggling state
  const [togglingId, setTogglingId] = useState<string | null>(null);

  const selectedServer = servers.find((s) => s.id === selectedId) || null;

  // Save Composio Key
  const handleSaveComposioKey = async (e: React.FormEvent) => {
    e.preventDefault();
    const key = apiKeyInput.trim();
    if (!key) {
      toast.error("Please enter a valid Composio API key");
      return;
    }
    setSavingKey(true);
    try {
      const res = await saveComposioSettings(key);
      onComposioUpdated({ configured: true, masked_key: res.masked_key });
      setApiKeyInput("");
      setShowKeyInput(false);
      toast.success("Composio API key saved! 1-Click connect is now active for all 250+ apps.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to save Composio key");
    } finally {
      setSavingKey(false);
    }
  };

  // Delete Composio Key
  const handleDeleteComposioKey = async () => {
    if (
      !confirm(
        "Remove stored Composio API key? You will need to enter it again to use 1-click cloud login.",
      )
    ) {
      return;
    }
    setDeletingKey(true);
    try {
      await deleteComposioSettings();
      onComposioUpdated({ configured: false, masked_key: null });
      setShowKeyInput(true);
      toast.success("Composio API key removed");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to remove Composio key");
    } finally {
      setDeletingKey(false);
    }
  };

  // Toggle MCP Server runtime enablement
  const handleToggleServer = async (serverId: string, nextChecked: boolean) => {
    setTogglingId(serverId);
    try {
      const updated = await toggleMCPServer(serverId);
      onServerUpdated(updated);
      toast.success(updated.enabled ? `Enabled ${updated.name}` : `Disabled ${updated.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to toggle MCP server");
    } finally {
      setTogglingId(null);
    }
  };

  // Create custom MCP server
  const handleCreateServer = async (e: React.FormEvent) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name) {
      toast.error("Please provide a name for the MCP server");
      return;
    }

    setCreatingServer(true);
    try {
      const isStdio = selectedCatalogType === "mcp-stdio";
      const payload = isStdio
        ? {
            name,
            server_url: "",
            transport: "stdio" as const,
            command: newCommand.trim(),
            args: newArgs.trim() ? newArgs.trim().split(/\s+/) : [],
          }
        : {
            name,
            server_url: newServerUrl.trim(),
            transport: newTransport,
            auth_token: newAuthToken.trim() || undefined,
          };

      const created = await createCustomMCPServer(payload);
      toast.success(`MCP Server ${created.name} connected!`);
      onServerCreated?.(created);
      onCancelCreate();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to connect MCP server");
    } finally {
      setCreatingServer(false);
    }
  };

  // ==========================================
  // CASE 1: Empty state (System Navigator)
  // Matches Image 1, 3, 5 with exact pixel parity
  // ==========================================
  if (!selectedId && !showCreateFlow) {
    return (
      <div className="flex h-full flex-col items-center justify-center space-y-6 py-12 text-center">
        <div className="bg-primary/10 text-primary rounded-3xl p-6 shadow-[0_0_40px_rgba(var(--primary-rgb),0.1)]">
          <Zap size={48} className="animate-pulse" />
        </div>
        <div className="max-w-sm space-y-3 px-4">
          <h3 className="text-xl font-bold tracking-tight">System Navigator</h3>
          <p className="text-muted-foreground text-sm leading-relaxed">
            Select a provider from the inventory to adjust its parameters or test connectivity.
          </p>
          {isNarrowViewport && !isInventoryExpanded && (
            <button
              onClick={onOpenInventory}
              className="theme-chip bg-primary mt-6 rounded-xl px-8 py-3 text-[10px] font-bold tracking-widest text-slate-950 uppercase"
            >
              Open Inventory
            </button>
          )}
        </div>
      </div>
    );
  }

  // ==========================================
  // CASE 2: Create Custom Server Flow
  // ==========================================
  if (showCreateFlow) {
    const isStdio = selectedCatalogType === "mcp-stdio";

    return (
      <div className="space-y-6 pb-6">
        <div className="flex items-center justify-between border-b border-glass-border pb-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-glass-border bg-surface-1/90 text-primary">
              {isStdio ? <Terminal size={20} /> : <Globe size={20} />}
            </div>
            <div>
              <h2 className="text-foreground text-lg font-bold">
                {isStdio ? "Connect Local Stdio MCP Server" : "Connect Remote MCP Server"}
              </h2>
              <p className="text-muted-foreground text-xs">
                {isStdio
                  ? "Launch and communicate with a local CLI, Python, or Node process via stdio"
                  : "Connect an external MCP server running Streamable HTTP or Server-Sent Events"}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onCancelCreate}
            className="rounded-xl border border-glass-border px-3.5 py-1.5 text-xs font-semibold text-muted-foreground hover:text-foreground hover:bg-surface-1 transition-all"
          >
            Cancel
          </button>
        </div>

        <form onSubmit={handleCreateServer} className="space-y-5 max-w-xl">
          <div>
            <label className="text-muted-foreground font-mono text-[10px] font-bold tracking-[0.2em] uppercase block mb-2">
              Server Name
            </label>
            <input
              type="text"
              required
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={isStdio ? "e.g. Local SQLite / Postgres MCP" : "e.g. Remote Knowledge MCP"}
              className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-4 py-2.5 text-xs text-foreground placeholder:text-muted-foreground/40 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary transition-all"
            />
          </div>

          {isStdio ? (
            <>
              <div>
                <label className="text-muted-foreground font-mono text-[10px] font-bold tracking-[0.2em] uppercase block mb-2">
                  Command
                </label>
                <input
                  type="text"
                  required
                  value={newCommand}
                  onChange={(e) => setNewCommand(e.target.value)}
                  placeholder="e.g. npx, python, node, /usr/local/bin/mcp-server"
                  className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-4 py-2.5 text-xs text-foreground font-mono placeholder:text-muted-foreground/40 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary transition-all"
                />
              </div>

              <div>
                <label className="text-muted-foreground font-mono text-[10px] font-bold tracking-[0.2em] uppercase block mb-2">
                  Arguments (space-separated)
                </label>
                <input
                  type="text"
                  value={newArgs}
                  onChange={(e) => setNewArgs(e.target.value)}
                  placeholder="e.g. -y @modelcontextprotocol/server-postgres postgresql://..."
                  className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-4 py-2.5 text-xs text-foreground font-mono placeholder:text-muted-foreground/40 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary transition-all"
                />
              </div>
            </>
          ) : (
            <>
              <div>
                <label className="text-muted-foreground font-mono text-[10px] font-bold tracking-[0.2em] uppercase block mb-2">
                  Server Endpoint URL
                </label>
                <input
                  type="url"
                  required
                  value={newServerUrl}
                  onChange={(e) => setNewServerUrl(e.target.value)}
                  placeholder="e.g. https://mcp.internal.company.com/sse"
                  className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-4 py-2.5 text-xs text-foreground font-mono placeholder:text-muted-foreground/40 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary transition-all"
                />
              </div>

              <div>
                <label className="text-muted-foreground font-mono text-[10px] font-bold tracking-[0.2em] uppercase block mb-2">
                  Transport Protocol
                </label>
                <div className="flex gap-3">
                  <label className="flex items-center gap-2 text-xs text-foreground cursor-pointer">
                    <input
                      type="radio"
                      name="transport"
                      value="streamable_http"
                      checked={newTransport === "streamable_http"}
                      onChange={() => setNewTransport("streamable_http")}
                      className="accent-primary"
                    />
                    <span>Streamable HTTP</span>
                  </label>
                  <label className="flex items-center gap-2 text-xs text-foreground cursor-pointer">
                    <input
                      type="radio"
                      name="transport"
                      value="sse"
                      checked={newTransport === "sse"}
                      onChange={() => setNewTransport("sse")}
                      className="accent-primary"
                    />
                    <span>Server-Sent Events (SSE)</span>
                  </label>
                </div>
              </div>

              <div>
                <label className="text-muted-foreground font-mono text-[10px] font-bold tracking-[0.2em] uppercase block mb-2">
                  Bearer Token / Secret (Optional)
                </label>
                <input
                  type="password"
                  value={newAuthToken}
                  onChange={(e) => setNewAuthToken(e.target.value)}
                  placeholder="Optional Authorization header token"
                  className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-4 py-2.5 text-xs text-foreground font-mono placeholder:text-muted-foreground/40 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary transition-all"
                />
              </div>
            </>
          )}

          <div className="flex items-center gap-3 pt-2">
            <button
              type="submit"
              disabled={creatingServer}
              className="theme-chip group hover-yellow inline-flex items-center gap-2 rounded-xl bg-primary hover:bg-primary/90 text-slate-950 px-5 py-2.5 text-xs font-bold tracking-wider uppercase transition-all shadow-[0_0_15px_rgba(250,204,21,0.25)] disabled:opacity-40"
            >
              {creatingServer ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Connecting Server…
                </>
              ) : (
                <>
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  Connect MCP Server
                </>
              )}
            </button>
            <button
              type="button"
              onClick={onCancelCreate}
              className="rounded-xl border border-glass-border px-4 py-2.5 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-surface-1 transition-all"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    );
  }

  // ==========================================
  // CASE 3: Composio MCP Provider Configuration
  // ==========================================
  if (selectedId === "composio") {
    return (
      <div className="space-y-6 pb-6">
        {/* Banner Section */}
        <div className="relative overflow-hidden rounded-[1.5rem] border border-border/80 dark:border-white/10 bg-card/95 p-6 shadow-sm transition-all duration-300">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex items-start gap-4">
              <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border border-cyan-500/30 bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 shadow-sm">
                <ComposioLogo className="h-6 w-6" />
              </div>
              <div className="space-y-1">
                <div className="flex flex-wrap items-center gap-2.5">
                  <h2 className="text-foreground text-xl font-bold tracking-tight">
                    Composio Provider
                  </h2>
                  <span className="rounded-full border border-border/70 dark:border-white/10 bg-muted/80 px-2.5 py-0.5 text-[9px] font-bold tracking-wider uppercase text-muted-foreground">
                    Managed Cloud Provider
                  </span>
                  {composioSettings.configured ? (
                    <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-0.5 text-xs font-semibold text-emerald-700 dark:text-emerald-300 shadow-[0_0_8px_rgba(16,185,129,0.2)]">
                      <span className="h-2 w-2 rounded-full bg-emerald-500 dark:bg-emerald-400 animate-pulse" />
                      Connected
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-600/55 bg-amber-500/15 px-3 py-0.5 text-xs font-semibold text-amber-900 dark:text-amber-200">
                      Setup Required
                    </span>
                  )}
                </div>
                <p className="text-muted-foreground max-w-2xl text-xs leading-relaxed">
                  Enterprise cloud MCP provider for autonomous agent workflows. Configure your Composio
                  API key once to enable authenticated cloud tool execution.
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2 shrink-0">
              <Link
                href="/dashboard/mcp"
                className="group inline-flex items-center gap-2 rounded-xl border border-border/80 dark:border-white/10 bg-surface-1 hover:bg-surface-2 text-foreground px-4 py-2 text-xs font-bold uppercase tracking-wider hover:border-cyan-500/40 transition-all shadow-sm"
              >
                <span>Go to MCP Servers</span>
                <ArrowRight className="h-3.5 w-3.5 transition-transform duration-200 group-hover:translate-x-0.5" />
              </Link>
            </div>
          </div>
        </div>

        {/* API Key Configuration Card */}
        <div className="relative rounded-[1.25rem] border border-border/80 dark:border-white/10 bg-card p-6 space-y-5 shadow-sm">
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-cyan-500/30 bg-cyan-500/10 text-cyan-600 dark:text-cyan-400">
              <KeyRound className="h-5 w-5" />
            </div>
            <div>
              <h3 className="text-foreground text-base font-bold tracking-tight">
                Composio API Key Configuration
              </h3>
              <p className="text-muted-foreground text-xs mt-0.5">
                Your key is encrypted locally and stored only in your local machine database (
                <code className="font-mono text-[11px] text-foreground font-semibold bg-muted px-1.5 py-0.5 rounded border border-border/60">
                  ~/.averqel-harness/secrets/composio_key.txt
                </code>
                ). It never leaves your system.
              </p>
            </div>
          </div>

          {composioSettings.configured && !showKeyInput ? (
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-5">
              <div className="flex items-center gap-3.5">
                <CheckCircle2 className="h-5 w-5 text-emerald-500 dark:text-emerald-400 shrink-0" />
                <div>
                  <div className="text-sm font-semibold text-foreground flex flex-wrap items-center gap-2.5">
                    <span>API Key Configured & Active</span>
                    <span className="font-mono text-xs font-bold text-emerald-600 dark:text-emerald-300 bg-background/90 border border-emerald-500/30 px-2.5 py-0.5 rounded-lg">
                      {composioSettings.masked_key || "••••••••••••"}
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    1-Click connect is enabled. You can connect any MCP app immediately from the
                    marketplace.
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-2 shrink-0">
                <button
                  type="button"
                  onClick={() => setShowKeyInput(true)}
                  className="rounded-xl border border-border/80 dark:border-white/10 bg-surface-1 hover:bg-surface-2 px-4 py-2 text-xs font-bold uppercase tracking-wider text-foreground hover:border-cyan-500/40 transition-all shadow-sm"
                >
                  Change Key
                </button>
                <button
                  type="button"
                  disabled={deletingKey}
                  onClick={handleDeleteComposioKey}
                  className="flex items-center gap-1.5 rounded-xl border border-rose-500/30 bg-rose-500/10 hover:bg-rose-500/20 px-4 py-2 text-xs font-bold uppercase tracking-wider text-rose-600 dark:text-rose-300 transition-all shadow-sm"
                >
                  {deletingKey ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Trash2 className="h-3.5 w-3.5" />
                  )}
                  Remove
                </button>
              </div>
            </div>
          ) : (
            <form onSubmit={handleSaveComposioKey} className="space-y-4 max-w-xl">
              <div>
                <label htmlFor="composio-api-key" className="text-slate-800 dark:text-slate-100 font-mono text-[10px] font-bold tracking-[0.2em] uppercase block mb-2">
                  Composio API Key
                </label>
                <div className="relative">
                  <KeyRound className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-600 dark:text-slate-300" />
                  <input
                    id="composio-api-key"
                    className="provider-api-key-input w-full rounded-xl border border-slate-400 bg-white py-3 pr-11 pl-10 font-mono text-xs text-slate-900 shadow-sm placeholder:text-slate-500 transition-all focus:border-cyan-600 focus:ring-2 focus:ring-cyan-500/25 focus:outline-none dark:border-slate-500 dark:bg-[#131b15] dark:text-slate-100 dark:placeholder:text-slate-400 dark:focus:border-cyan-400"
                    type={showSecret ? "text" : "password"}
                    required
                    value={apiKeyInput}
                    onChange={(e) => setApiKeyInput(e.target.value)}
                    placeholder="Paste your key here (e.g. comp_...)"
                  />
                  <button
                    type="button"
                    onClick={() => setShowSecret(!showSecret)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 rounded-md p-1 text-slate-600 transition-colors hover:bg-surface-2 hover:text-slate-950 focus:outline-none dark:text-slate-300 dark:hover:text-white"
                    title={showSecret ? "Hide API key" : "Show API key"}
                  >
                    {showSecret ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2 mt-2.5 text-xs">
                  <span className="text-muted-foreground text-[11px]">
                    Stored securely in local secrets file.
                  </span>
                  <a
                    href="https://app.composio.dev"
                    target="_blank"
                    rel="noreferrer noopener"
                    className="inline-flex items-center gap-1 text-cyan-600 dark:text-cyan-400 hover:underline font-semibold transition-colors"
                  >
                    <span>Get a free Composio API key ↗</span>
                    <ExternalLink className="h-3 w-3" />
                  </a>
                </div>
              </div>

              <div className="flex items-center gap-2.5 pt-1">
                <button
                  type="submit"
                  disabled={savingKey || !apiKeyInput.trim()}
                  className="group inline-flex items-center gap-2 rounded-xl bg-cyan-600 hover:bg-cyan-500 text-white dark:bg-cyan-500 dark:hover:bg-cyan-400 dark:text-slate-950 px-5 py-2.5 text-xs font-bold tracking-wider uppercase transition-all shadow-md shadow-cyan-500/20 disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground disabled:shadow-none disabled:opacity-60"
                >
                  {savingKey ? (
                    <>
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      Saving & Activating…
                    </>
                  ) : (
                    <>
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      Save Key & Enable 1-Click Connect
                    </>
                  )}
                </button>
                {composioSettings.configured && (
                  <button
                    type="button"
                    onClick={() => {
                      setShowKeyInput(false);
                      setApiKeyInput("");
                    }}
                    className="rounded-xl border border-border/80 dark:border-white/10 px-4 py-2.5 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-surface-1 transition-all"
                  >
                    Cancel
                  </button>
                )}
              </div>
            </form>
          )}
        </div>

        {/* Provider Connectivity & Architecture Parameters */}
        <div className="relative rounded-[1.25rem] border border-border/80 dark:border-white/10 bg-card p-6 space-y-4 shadow-sm">
          <div className="flex items-center gap-2">
            <h3 className="text-xs font-bold text-foreground uppercase tracking-wider font-mono">
              Provider Architecture & Connectivity
            </h3>
          </div>

          <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 text-xs">
            <div className="rounded-xl border border-border/70 dark:border-white/10 bg-surface-1/70 p-4 space-y-1.5 transition-all hover:border-cyan-500/30">
              <div className="flex items-center gap-1.5 text-muted-foreground font-mono text-[10px] uppercase tracking-wider">
                <Globe className="h-3.5 w-3.5 text-cyan-600 dark:text-cyan-400" />
                <span>Provider Cloud Endpoint</span>
              </div>
              <code className="text-foreground font-mono text-xs break-all block font-medium">
                https://backend.composio.dev/api/v1
              </code>
            </div>

            <div className="rounded-xl border border-border/70 dark:border-white/10 bg-surface-1/70 p-4 space-y-1.5 transition-all hover:border-cyan-500/30">
              <div className="flex items-center gap-1.5 text-muted-foreground font-mono text-[10px] uppercase tracking-wider">
                <ShieldCheck className="h-3.5 w-3.5 text-cyan-600 dark:text-cyan-400" />
                <span>Authentication Mode</span>
              </div>
              <span className="text-foreground text-xs font-semibold block">
                API Key (Bearer Token Authentication)
              </span>
            </div>

            <div className="rounded-xl border border-border/70 dark:border-white/10 bg-surface-1/70 p-4 space-y-1.5 transition-all hover:border-cyan-500/30">
              <div className="flex items-center gap-1.5 text-muted-foreground font-mono text-[10px] uppercase tracking-wider">
                <Cpu className="h-3.5 w-3.5 text-cyan-600 dark:text-cyan-400" />
                <span>Protocol & Execution</span>
              </div>
              <span className="text-foreground text-xs block font-medium">
                Streamable HTTP · Remote Agent Bridge
              </span>
            </div>

            <div className="rounded-xl border border-border/70 dark:border-white/10 bg-surface-1/70 p-4 space-y-1.5 transition-all hover:border-cyan-500/30">
              <div className="flex items-center gap-1.5 text-muted-foreground font-mono text-[10px] uppercase tracking-wider">
                <Lock className="h-3.5 w-3.5 text-cyan-600 dark:text-cyan-400" />
                <span>Secrets Storage</span>
              </div>
              <span className="text-foreground text-xs block font-medium">
                Local Machine Encrypted Database (~/.averqel-harness)
              </span>
            </div>
          </div>
        </div>

        {/* Dedicated MCP Server & Marketplace Bridge Callout */}
        <div className="rounded-2xl border border-border/80 dark:border-white/10 bg-card p-6 transition-all shadow-sm">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div className="flex items-start gap-3.5">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-cyan-500/30 bg-cyan-500/10 text-cyan-600 dark:text-cyan-400">
                <Layers className="h-5 w-5" />
              </div>
              <div className="space-y-1">
                <h4 className="text-foreground text-sm font-bold tracking-tight">
                  Connect & Use Individual MCP Server Apps
                </h4>
                <p className="text-muted-foreground text-xs max-w-xl leading-relaxed">
                  Individual tool applications (such as Google Drive, Slack, GitHub, Notion, PostgreSQL, and Jira)
                  are installed, authorized, and used on the dedicated <span className="text-foreground font-semibold">MCP Servers</span> page.
                </p>
              </div>
            </div>

            <Link
              href="/dashboard/mcp"
              className="group inline-flex items-center gap-2 rounded-xl bg-foreground text-background hover:bg-foreground/90 px-5 py-2.5 text-xs font-bold tracking-wider uppercase transition-all shadow-sm shrink-0 self-start sm:self-auto"
            >
              <span>Open MCP Servers Page</span>
              <ArrowRight className="h-3.5 w-3.5 transition-transform duration-200 group-hover:translate-x-0.5" />
            </Link>
          </div>
        </div>
      </div>
    );
  }

  // ==========================================
  // CASE 4: Selected Connected Server Detail
  // ==========================================
  if (selectedServer) {
    const isConnected = selectedServer.status === "connected";
    const isNeedsAuth = selectedServer.status === "needs_auth";
    const isLocal = selectedServer.transport === "stdio";
    const toolCount = Number(selectedServer.config?.mcp_catalog_tool_count || 0);

    return (
      <div className="space-y-6 pb-6">
        {/* Server Header Card */}
        <div className="theme-card relative rounded-[1.5rem] border border-glass-border bg-surface-0/95 p-6 shadow-sm">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex items-start gap-4">
              <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border border-glass-border bg-surface-1/90 p-2 shadow-sm">
                <MCPLogo
                  entry={{
                    id: selectedServer.id,
                    name: selectedServer.name,
                    server_name: selectedServer.name,
                    provider_slug: selectedServer.config?.provider_slug || selectedServer.name,
                    trusted_logo_key: selectedServer.name,
                  }}
                  size={36}
                  className="rounded-xl border-0 bg-transparent p-0 shadow-none"
                />
              </div>
              <div className="space-y-1">
                <div className="flex flex-wrap items-center gap-2.5">
                  <h2 className="text-foreground text-xl font-bold tracking-tight">
                    {selectedServer.name}
                  </h2>
                  <span
                    className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[9px] font-bold tracking-wider uppercase ${
                      isConnected
                        ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.2)]"
                        : isNeedsAuth
                          ? "border-amber-500/40 bg-amber-500/10 text-amber-300"
                          : "border-rose-500/40 bg-rose-500/10 text-rose-300"
                    }`}
                  >
                    {isConnected && (
                      <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                    )}
                    {selectedServer.status.replace("_", " ")}
                  </span>
                  <span className="rounded-full border border-glass-border bg-muted px-2 py-0.5 text-[8px] font-bold tracking-wider uppercase text-muted-foreground">
                    {isLocal ? "Local" : "Hosted"}
                  </span>
                </div>
                <p className="text-muted-foreground text-xs">
                  Transport: <span className="font-mono text-foreground">{selectedServer.transport.toUpperCase()}</span>
                  {" · "}
                  Execution: <span className="text-foreground">{isLocal ? "Local machine" : "Remote endpoint"}</span>
                </p>
              </div>
            </div>

            {/* Live Toggle Switch */}
            <div className="flex items-center gap-3 self-start sm:self-auto">
              <span className="text-xs font-medium text-muted-foreground">
                {selectedServer.enabled !== false ? "Active in Runtime" : "Disabled"}
              </span>
              <MCPToggleSwitch
                size="md"
                checked={selectedServer.enabled !== false}
                disabled={togglingId === selectedServer.id}
                onChange={(nextChecked) =>
                  handleToggleServer(selectedServer.id, nextChecked)
                }
              />
            </div>
          </div>

          {/* DeepSpace Engine Pill */}
          <div className="mt-5 flex items-center justify-between border-t border-glass-border pt-4">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-primary" />
              <span className="text-xs font-semibold text-foreground">
                {toolCount} tools active in DeepSpace autonomous tool engine
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Link
                href={`/dashboard/mcp/settings/policies?server=${encodeURIComponent(selectedServer.id)}`}
                className="theme-chip hover:text-primary rounded-xl border border-glass-border bg-surface-1 px-3 py-1.5 text-[11px] font-bold tracking-wider uppercase text-foreground transition-all"
              >
                Configure Policy
              </Link>
              <Link
                href={`/dashboard/mcp/inspector?server=${encodeURIComponent(selectedServer.id)}`}
                className="theme-chip hover:text-primary rounded-xl border border-glass-border bg-surface-1 px-3 py-1.5 text-[11px] font-bold tracking-wider uppercase text-foreground transition-all"
              >
                Inspect Tools
              </Link>
            </div>
          </div>
        </div>

        {/* Server Specification & Configuration Details */}
        <div className="theme-card rounded-[1.25rem] border border-glass-border bg-surface-0 p-6 space-y-4">
          <h3 className="text-sm font-bold text-foreground uppercase tracking-wider font-mono">
            Connection Parameters
          </h3>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 text-xs">
            <div className="rounded-xl border border-glass-border bg-surface-1/60 p-4 space-y-1">
              <span className="text-muted-foreground font-mono text-[10px] uppercase tracking-wider block">
                Endpoint / Command
              </span>
              <code className="text-foreground font-mono text-xs break-all">
                {selectedServer.config?.server_url ||
                  selectedServer.config?.command ||
                  "Configured in server manifest"}
              </code>
            </div>

            <div className="rounded-xl border border-glass-border bg-surface-1/60 p-4 space-y-1">
              <span className="text-muted-foreground font-mono text-[10px] uppercase tracking-wider block">
                Identity & Scope
              </span>
              <span className="text-foreground text-xs">
                {selectedServer.account_identity?.email ||
                  selectedServer.account_identity?.display_name ||
                  "Local System Process"}
              </span>
            </div>
          </div>

          {selectedServer.last_error && (
            <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-xs text-rose-300 space-y-1">
              <span className="font-bold flex items-center gap-1.5">
                <AlertCircle size={14} /> Last Error
              </span>
              <p className="font-mono text-[11px]">{selectedServer.last_error}</p>
            </div>
          )}

          {/* Action Row */}
          <div className="flex items-center justify-between pt-4 border-t border-glass-border">
            <span className="text-[11px] text-muted-foreground">
              To remove or disconnect this connection, click Disconnect.
            </span>
            {onDeleteServer && (
              <button
                type="button"
                onClick={() => onDeleteServer(selectedServer)}
                className="flex items-center gap-1.5 rounded-xl border border-rose-500/30 bg-rose-500/10 hover:bg-rose-500/20 px-4 py-2 text-xs font-bold uppercase tracking-wider text-rose-300 transition-all"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Disconnect Server
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }

  return null;
}

"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Sparkles,
  Key,
  CheckCircle2,
  AlertCircle,
  ExternalLink,
  Trash2,
  Server,
  Layers,
  ArrowRight,
  ShieldCheck,
  RefreshCw,
} from "lucide-react";
import toast from "react-hot-toast";

import {
  getComposioSettings,
  saveComposioSettings,
  deleteComposioSettings,
  listMCPServers,
  toggleMCPServer,
  type MCPConnection,
} from "@/lib/mcp-api";
import { MCPLogo } from "@/app/dashboard/mcp/_components/MCPMarketplaceCard";
import MCPToggleSwitch from "@/app/dashboard/mcp/_components/MCPToggleSwitch";

interface PopularAppMeta {
  name: string;
  slug: string;
}

const POPULAR_APPS: PopularAppMeta[] = [
  { name: "Google Drive", slug: "google-drive" },
  { name: "Gmail", slug: "gmail" },
  { name: "Calendar", slug: "google-calendar" },
  { name: "GitHub", slug: "github" },
  { name: "Notion", slug: "notion" },
  { name: "Slack", slug: "slack" },
  { name: "Jira", slug: "jira" },
  { name: "Linear", slug: "linear" },
  { name: "Salesforce", slug: "salesforce" },
  { name: "Trello", slug: "trello" },
  { name: "Asana", slug: "asana" },
  { name: "HubSpot", slug: "hubspot" },
  { name: "Discord", slug: "discord" },
  { name: "Zoom", slug: "zoom" },
  { name: "Airtable", slug: "airtable" },
  { name: "Zendesk", slug: "zendesk" },
];

export default function MCPSettingsPanel() {
  const [loading, setLoading] = useState(true);
  const [configured, setConfigured] = useState(false);
  const [maskedKey, setMaskedKey] = useState<string | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [showInput, setShowInput] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [servers, setServers] = useState<MCPConnection[]>([]);

  const loadData = async () => {
    try {
      const [settings, serverList] = await Promise.all([
        getComposioSettings().catch(() => ({ configured: false, masked_key: null })),
        listMCPServers().catch(() => []),
      ]);
      setConfigured(Boolean(settings.configured));
      setMaskedKey(settings.masked_key || null);
      setServers(serverList || []);
      if (!settings.configured) {
        setShowInput(true);
      }
    } catch {
      toast.error("Failed to load MCP settings");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    queueMicrotask(() => void loadData());
  }, []);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    const key = apiKeyInput.trim();
    if (!key) {
      toast.error("Please enter a valid Composio API key");
      return;
    }
    setSaving(true);
    try {
      const res = await saveComposioSettings(key);
      setConfigured(true);
      setMaskedKey(res.masked_key);
      setApiKeyInput("");
      setShowInput(false);
      toast.success("Composio API key saved! 1-Click connect is now active for all 250+ apps.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to save Composio key");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (
      !confirm(
        "Remove stored Composio API key? You will need to enter it again to use 1-click cloud login.",
      )
    ) {
      return;
    }
    setDeleting(true);
    try {
      await deleteComposioSettings();
      setConfigured(false);
      setMaskedKey(null);
      setShowInput(true);
      toast.success("Composio API key removed");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to remove Composio key");
    } finally {
      setDeleting(false);
    }
  };

  const handleToggleServer = async (serverId: string, nextChecked: boolean) => {
    // Optimistic UI update
    setServers((prev) => prev.map((s) => (s.id === serverId ? { ...s, enabled: nextChecked } : s)));
    setTogglingId(serverId);
    try {
      const updated = await toggleMCPServer(serverId);
      setServers((prev) =>
        prev.map((s) => (s.id === serverId ? { ...s, enabled: updated.enabled } : s)),
      );
      toast.success(updated.enabled ? `Enabled ${updated.name}` : `Disabled ${updated.name}`);
    } catch (err) {
      // Revert on error
      setServers((prev) =>
        prev.map((s) => (s.id === serverId ? { ...s, enabled: !nextChecked } : s)),
      );
      toast.error(err instanceof Error ? err.message : "Failed to toggle MCP server");
    } finally {
      setTogglingId(null);
    }
  };

  if (loading) {
    return (
      <div className="col-span-12 flex min-h-[18rem] items-center justify-center">
        <div className="text-muted-foreground flex items-center gap-2.5 text-sm">
          <AverQelLoader className="text-primary h-4 w-4 animate-spin" />
          <span>Loading MCP provider settings…</span>
        </div>
      </div>
    );
  }

  return (
    <div className="col-span-12 space-y-6 pb-12">
      {/* Top Section Banner: Provider Native Theme Card */}
      <div className="theme-card border-glass-border bg-surface-0/95 relative overflow-hidden rounded-[1.5rem] border p-6 shadow-sm transition-all duration-300 sm:p-7">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-start gap-4">
            <div className="border-glass-border bg-surface-1/90 text-primary flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border shadow-sm">
              <Sparkles className="h-6 w-6" />
            </div>
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2.5">
                <h2 className="text-foreground text-xl font-bold tracking-tight">
                  Composio MCP Provider
                </h2>
                {configured ? (
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-0.5 text-xs font-semibold text-emerald-700 shadow-[0_0_8px_rgba(16,185,129,0.2)] dark:text-emerald-300">
                    <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
                    1-Click Active
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-600/55 bg-amber-500/15 px-3 py-0.5 text-xs font-semibold text-amber-900 dark:text-amber-200">
                    Setup Required
                  </span>
                )}
              </div>
              <p className="text-muted-foreground max-w-2xl text-xs leading-relaxed">
                Add your Composio API key once. When saved, all{" "}
                <span className="text-foreground font-semibold">250+ enterprise apps</span> in the
                MCP Marketplace can be connected with a single click—zero Google Cloud Console,
                Slack App, or OAuth developer portal setup required.
              </p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <Link
              href="/dashboard/mcp"
              className="theme-chip group hover-yellow border-glass-border bg-surface-1 text-foreground hover:border-primary/40 hover:bg-surface-2 inline-flex items-center gap-2 rounded-xl border px-4 py-2.5 text-xs font-bold tracking-wider uppercase transition-all"
            >
              <span>Browse 311 Apps</span>
              <ArrowRight className="h-3.5 w-3.5 transition-transform duration-200 group-hover:translate-x-0.5" />
            </Link>
          </div>
        </div>

        {/* Supported 1-Click Apps with real native brand logos */}
        <div className="border-glass-border mt-6 border-t pt-4">
          <p className="text-muted-foreground mb-3 font-mono text-[9px] font-bold tracking-[0.24em] uppercase">
            Supported 1-Click Apps ({POPULAR_APPS.length} of 250+):
          </p>
          <div className="flex flex-wrap gap-2">
            {POPULAR_APPS.map((app) => (
              <Link
                key={app.name}
                href={`/dashboard/mcp?q=${encodeURIComponent(app.name)}`}
                className="theme-chip group hover-yellow border-glass-border bg-surface-1/70 text-foreground hover:border-primary/40 hover:bg-surface-2 inline-flex items-center gap-2 rounded-xl border px-3 py-1.5 text-xs font-medium transition-all duration-200 hover:shadow-[0_2px_12px_-4px_hsl(var(--primary)/0.25)]"
                title={`Search ${app.name} in Marketplace`}
              >
                <MCPLogo
                  entry={{ name: app.name, provider_slug: app.slug }}
                  size={16}
                  className="rounded-md border-0 bg-transparent p-0 shadow-none"
                />
                <span>{app.name}</span>
              </Link>
            ))}
            <Link
              href="/dashboard/mcp"
              className="theme-chip border-primary/30 bg-primary/10 text-primary hover:bg-primary/20 inline-flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-xs font-semibold transition-all"
            >
              + 235 more apps →
            </Link>
          </div>
        </div>
      </div>

      {/* Main Configuration Card: Provider Native Theme */}
      <div className="theme-card border-glass-border bg-surface-0 relative space-y-5 rounded-[1.25rem] border p-6 shadow-sm">
        <div className="flex items-start gap-3">
          <div className="border-glass-border bg-surface-1/80 text-primary flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border">
            <Key className="h-5 w-5" />
          </div>
          <div>
            <h3 className="text-foreground text-base font-bold tracking-tight">
              Composio API Key Configuration
            </h3>
            <p className="text-muted-foreground mt-0.5 text-xs">
              Your key is encrypted locally and stored only in your local machine database (
              <code className="text-foreground/80 font-mono text-[11px]">
                ~/.averqel-harness/secrets/composio_key.txt
              </code>
              ). It never leaves your system.
            </p>
          </div>
        </div>

        {configured && !showInput ? (
          <div className="flex flex-col justify-between gap-4 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-5 sm:flex-row sm:items-center">
            <div className="flex items-center gap-3.5">
              <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-400" />
              <div>
                <div className="flex items-center gap-2.5 text-sm font-semibold text-emerald-100">
                  <span>API Key Configured & Active</span>
                  <span className="bg-surface-0 rounded-lg border border-emerald-500/30 px-2.5 py-0.5 font-mono text-xs text-emerald-300">
                    {maskedKey || "••••••••"}
                  </span>
                </div>
                <p className="mt-1 text-xs text-emerald-200/70">
                  1-Click connect is enabled. You can connect any MCP app immediately from the
                  marketplace.
                </p>
              </div>
            </div>

            <div className="flex shrink-0 items-center gap-2">
              <button
                type="button"
                onClick={() => setShowInput(true)}
                className="theme-chip hover:text-primary border-glass-border bg-surface-0/80 hover:bg-surface-1 text-foreground rounded-xl border px-4 py-2 text-xs font-bold tracking-wider uppercase transition-all"
              >
                Change Key
              </button>
              <button
                type="button"
                disabled={deleting}
                onClick={handleDelete}
                className="flex items-center gap-1.5 rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-2 text-xs font-bold tracking-wider text-rose-300 uppercase transition-all hover:bg-rose-500/20"
              >
                {deleting ? (
                  <AverQelLoader className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Trash2 className="h-3.5 w-3.5" />
                )}
                Remove
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSave} className="max-w-xl space-y-4">
            <div>
              <label
                htmlFor="composio-api-key-settings"
                className="mb-2 block font-mono text-[10px] font-bold tracking-[0.2em] text-slate-800 uppercase dark:text-slate-100"
              >
                Composio API Key
              </label>
              <div className="relative">
                <Key className="pointer-events-none absolute top-3.5 left-3.5 h-4 w-4 text-slate-600 dark:text-slate-300" />
                <input
                  id="composio-api-key-settings"
                  className="provider-api-key-input focus:border-primary focus:ring-primary/20 w-full rounded-xl border border-slate-400 bg-white py-3 pr-4 pl-10 font-mono text-xs text-slate-900 shadow-sm transition-all placeholder:text-slate-500 focus:ring-2 focus:outline-none dark:border-slate-500 dark:bg-[#131b15] dark:text-slate-100 dark:placeholder:text-slate-400"
                  type="password"
                  required
                  value={apiKeyInput}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                  placeholder="Paste your key here (e.g. comp_...)"
                />
              </div>
              <div className="mt-2.5 flex items-center justify-between text-xs">
                <span className="text-muted-foreground text-[11px]">
                  Stored securely in local secrets file.
                </span>
                <a
                  href="https://app.composio.dev"
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-primary hover:text-primary/80 decoration-primary/30 inline-flex items-center gap-1 font-medium underline transition-colors"
                >
                  Get a free Composio API key ↗
                  <ExternalLink className="h-3 w-3" />
                </a>
              </div>
            </div>

            <div className="flex items-center gap-2.5 pt-1">
              <button
                type="submit"
                disabled={saving || !apiKeyInput.trim()}
                className="theme-chip group hover-yellow bg-primary hover:bg-primary/90 inline-flex items-center gap-2 rounded-xl px-5 py-2.5 text-xs font-bold tracking-wider text-slate-950 uppercase shadow-[0_0_15px_rgba(250,204,21,0.25)] transition-all disabled:cursor-not-allowed disabled:opacity-40"
              >
                {saving ? (
                  <>
                    <AverQelLoader className="h-3.5 w-3.5 animate-spin" />
                    Saving & Activating…
                  </>
                ) : (
                  <>
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Save Key & Enable 1-Click Connect
                  </>
                )}
              </button>
              {configured && (
                <button
                  type="button"
                  onClick={() => {
                    setShowInput(false);
                    setApiKeyInput("");
                  }}
                  className="border-glass-border text-muted-foreground hover:text-foreground hover:bg-surface-1 rounded-xl border px-4 py-2.5 text-xs font-medium transition-all"
                >
                  Cancel
                </button>
              )}
            </div>
          </form>
        )}
      </div>

      {/* Connected Servers & DeepSpace Runtime Overview: Provider Native Grid */}
      <div className="theme-card border-glass-border bg-surface-0 relative space-y-5 rounded-[1.25rem] border p-6 shadow-sm">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-muted-foreground font-mono text-[9px] font-bold tracking-[0.24em] uppercase">
              DeepSpace Tool Engine
            </p>
            <div className="mt-1 flex items-center gap-3">
              <Server className="text-primary h-5 w-5" />
              <h3 className="text-foreground text-lg font-bold tracking-tight">
                Connected MCP Servers & Tools
              </h3>
              <span className="theme-pill opacity-80">{servers.length} active</span>
            </div>
            <p className="text-muted-foreground mt-1 text-xs">
              Servers currently provisioned and active in DeepSpace for autonomous tool calling.
            </p>
          </div>
          <Link
            href="/dashboard/mcp"
            className="theme-chip group hover-yellow border-glass-border bg-surface-1 text-foreground hover:border-primary/40 hover:bg-surface-2 inline-flex items-center gap-2 self-start rounded-xl border px-4 py-2 text-xs font-bold tracking-wider uppercase transition-all sm:self-auto"
          >
            <Layers className="h-3.5 w-3.5" />
            Manage in Marketplace
          </Link>
        </div>

        {servers.length === 0 ? (
          <div className="border-glass-border bg-surface-0/50 space-y-3 rounded-2xl border border-dashed p-8 text-center">
            <p className="text-muted-foreground text-sm font-medium">
              No MCP servers connected yet
            </p>
            <p className="text-muted-foreground/70 mx-auto max-w-md text-xs">
              Once you connect apps from the MCP Marketplace or add custom stdio / HTTP endpoints,
              they will appear here with live tool permissions.
            </p>
            <div className="pt-2">
              <Link
                href="/dashboard/mcp"
                className="theme-chip group hover-yellow bg-primary inline-flex items-center gap-2 rounded-xl px-5 py-2.5 text-xs font-bold tracking-wider text-slate-950 uppercase shadow-[0_0_15px_rgba(250,204,21,0.2)] transition-all"
              >
                Browse Marketplace Connectors
              </Link>
            </div>
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {servers.map((s) => {
              const isConnected = s.status === "connected";
              const isNeedsAuth = s.status === "needs_auth";
              const isFailed = s.status === "failed";
              const isEnabled = s.enabled !== false;
              const toolCount = Number(s.config?.mcp_catalog_tool_count || 0);

              return (
                <div
                  key={s.id}
                  className="theme-card group hover-yellow border-glass-border bg-surface-0 hover:border-primary/40 relative flex flex-col justify-between rounded-[1.25rem] border p-5 transition-all duration-300 hover:shadow-[0_4px_24px_-8px_hsl(var(--primary)/0.18)]"
                >
                  {/* Top Row: Squircle Logo + Status Badge & Toggle Switch */}
                  <div>
                    <div className="flex items-start justify-between gap-3">
                      <div className="border-glass-border bg-surface-1/80 flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl border p-2 shadow-sm">
                        <MCPLogo
                          entry={{
                            id: s.id,
                            name: s.name,
                            server_name: s.name,
                            provider_slug: s.config?.provider_slug || s.name,
                            trusted_logo_key: s.name,
                          }}
                          size={32}
                          className="rounded-xl border-0 bg-transparent p-0 shadow-none"
                        />
                      </div>

                      <div className="flex items-center gap-2.5">
                        {/* Status Badge */}
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
                            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
                          )}
                          {s.status.replace("_", " ")}
                        </span>

                        {/* Live Toggle Switch */}
                        <MCPToggleSwitch
                          size="sm"
                          checked={isEnabled}
                          disabled={togglingId === s.id}
                          onChange={(next) => void handleToggleServer(s.id, next)}
                          title={
                            isEnabled ? "Disable server in DeepSpace" : "Enable server in DeepSpace"
                          }
                        />
                      </div>
                    </div>

                    {/* Middle: Title, Transport Tag, Tools Count */}
                    <div className="mt-3.5 space-y-1.5">
                      <p className="text-muted-foreground font-mono text-[9px] font-bold tracking-[0.2em] uppercase">
                        {s.transport} · {s.config?.oauth_mode === "none" ? "Local" : "OAuth"}
                      </p>
                      <h4
                        className="text-foreground truncate text-base font-bold tracking-tight"
                        title={s.name}
                      >
                        {s.name}
                      </h4>
                      <div className="pt-1">
                        <span className="theme-chip border-glass-border bg-surface-1/70 text-muted-foreground inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 font-mono text-[11px]">
                          <span className="text-primary font-bold">⚡</span>
                          <span>{toolCount} tools active in DeepSpace</span>
                        </span>
                      </div>
                    </div>
                  </div>

                  {/* Footer: Configure Policy Link & Action */}
                  <div className="border-glass-border mt-4 flex items-center justify-between border-t pt-3">
                    <Link
                      href={`/dashboard/mcp/inspector/${encodeURIComponent(s.id)}`}
                      className="theme-chip hover:text-primary inline-flex items-center gap-1.5 text-[11px] font-bold tracking-wider uppercase transition-colors"
                    >
                      <span>Configure Policy</span>
                      <ArrowRight className="h-3 w-3" />
                    </Link>

                    <Link
                      href={`/dashboard/mcp`}
                      className="text-muted-foreground hover:text-foreground font-mono text-[10px] tracking-widest uppercase transition-colors"
                    >
                      Inspect
                    </Link>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

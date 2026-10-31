"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Sparkles,
  Key,
  CheckCircle2,
  AlertCircle,
  ExternalLink,
  Trash2,
  Loader2,
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
    setLoading(true);
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
    void loadData();
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
    if (!confirm("Remove stored Composio API key? You will need to enter it again to use 1-click cloud login.")) {
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
    setServers((prev) =>
      prev.map((s) => (s.id === serverId ? { ...s, enabled: nextChecked } : s)),
    );
    setTogglingId(serverId);
    try {
      const updated = await toggleMCPServer(serverId);
      setServers((prev) =>
        prev.map((s) => (s.id === serverId ? { ...s, enabled: updated.enabled } : s)),
      );
      toast.success(
        updated.enabled ? `Enabled ${updated.name}` : `Disabled ${updated.name}`,
      );
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
        <div className="flex items-center gap-2.5 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin text-primary" />
          <span>Loading MCP provider settings…</span>
        </div>
      </div>
    );
  }

  return (
    <div className="col-span-12 space-y-6 pb-12">
      {/* Top Section Banner: Provider Native Theme Card */}
      <div className="theme-card relative overflow-hidden rounded-[1.5rem] border border-glass-border bg-surface-0/95 p-6 sm:p-7 shadow-sm transition-all duration-300">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-start gap-4">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border border-glass-border bg-surface-1/90 text-primary shadow-sm">
              <Sparkles className="h-6 w-6" />
            </div>
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2.5">
                <h2 className="text-foreground text-xl font-bold tracking-tight">Composio MCP Provider</h2>
                {configured ? (
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-0.5 text-xs font-semibold text-emerald-700 dark:text-emerald-300 shadow-[0_0_8px_rgba(16,185,129,0.2)]">
                    <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
                    1-Click Active
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-600/55 bg-amber-500/15 px-3 py-0.5 text-xs font-semibold text-amber-900 dark:text-amber-200">
                    Setup Required
                  </span>
                )}
              </div>
              <p className="text-muted-foreground max-w-2xl text-xs leading-relaxed">
                Add your Composio API key once. When saved, all <span className="text-foreground font-semibold">250+ enterprise apps</span> in the MCP Marketplace can be connected with a single click—zero Google Cloud Console, Slack App, or OAuth developer portal setup required.
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <Link
              href="/dashboard/mcp"
              className="theme-chip group hover-yellow inline-flex items-center gap-2 rounded-xl border border-glass-border bg-surface-1 px-4 py-2.5 text-xs font-bold uppercase tracking-wider text-foreground hover:border-primary/40 hover:bg-surface-2 transition-all"
            >
              <span>Browse 311 Apps</span>
              <ArrowRight className="h-3.5 w-3.5 transition-transform duration-200 group-hover:translate-x-0.5" />
            </Link>
          </div>
        </div>

        {/* Supported 1-Click Apps with real native brand logos */}
        <div className="mt-6 border-t border-glass-border pt-4">
          <p className="text-muted-foreground font-mono text-[9px] font-bold tracking-[0.24em] uppercase mb-3">
            Supported 1-Click Apps ({POPULAR_APPS.length} of 250+):
          </p>
          <div className="flex flex-wrap gap-2">
            {POPULAR_APPS.map((app) => (
              <Link
                key={app.name}
                href={`/dashboard/mcp?q=${encodeURIComponent(app.name)}`}
                className="theme-chip group hover-yellow inline-flex items-center gap-2 rounded-xl border border-glass-border bg-surface-1/70 px-3 py-1.5 text-xs font-medium text-foreground transition-all duration-200 hover:border-primary/40 hover:bg-surface-2 hover:shadow-[0_2px_12px_-4px_hsl(var(--primary)/0.25)]"
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
              className="theme-chip inline-flex items-center gap-1.5 rounded-xl border border-primary/30 bg-primary/10 px-3 py-1.5 text-xs font-semibold text-primary hover:bg-primary/20 transition-all"
            >
              + 235 more apps →
            </Link>
          </div>
        </div>
      </div>

      {/* Main Configuration Card: Provider Native Theme */}
      <div className="theme-card relative rounded-[1.25rem] border border-glass-border bg-surface-0 p-6 space-y-5 shadow-sm">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-glass-border bg-surface-1/80 text-primary">
            <Key className="h-5 w-5" />
          </div>
          <div>
            <h3 className="text-foreground text-base font-bold tracking-tight">
              Composio API Key Configuration
            </h3>
            <p className="text-muted-foreground text-xs mt-0.5">
              Your key is encrypted locally and stored only in your local machine database (<code className="font-mono text-[11px] text-foreground/80">~/.averqel-harness/secrets/composio_key.txt</code>). It never leaves your system.
            </p>
          </div>
        </div>

        {configured && !showInput ? (
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-5">
            <div className="flex items-center gap-3.5">
              <CheckCircle2 className="h-5 w-5 text-emerald-400 shrink-0" />
              <div>
                <div className="text-sm font-semibold text-emerald-100 flex items-center gap-2.5">
                  <span>API Key Configured & Active</span>
                  <span className="font-mono text-xs text-emerald-300 bg-surface-0 border border-emerald-500/30 px-2.5 py-0.5 rounded-lg">
                    {maskedKey || "••••••••"}
                  </span>
                </div>
                <p className="text-xs text-emerald-200/70 mt-1">
                  1-Click connect is enabled. You can connect any MCP app immediately from the marketplace.
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2 shrink-0">
              <button
                type="button"
                onClick={() => setShowInput(true)}
                className="theme-chip hover:text-primary rounded-xl border border-glass-border bg-surface-0/80 hover:bg-surface-1 px-4 py-2 text-xs font-bold uppercase tracking-wider text-foreground transition-all"
              >
                Change Key
              </button>
              <button
                type="button"
                disabled={deleting}
                onClick={handleDelete}
                className="flex items-center gap-1.5 rounded-xl border border-rose-500/30 bg-rose-500/10 hover:bg-rose-500/20 px-4 py-2 text-xs font-bold uppercase tracking-wider text-rose-300 transition-all"
              >
                {deleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                Remove
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSave} className="space-y-4 max-w-xl">
            <div>
              <label htmlFor="composio-api-key-settings" className="text-slate-800 dark:text-slate-100 font-mono text-[10px] font-bold tracking-[0.2em] uppercase block mb-2">
                Composio API Key
              </label>
              <div className="relative">
                <Key className="pointer-events-none absolute left-3.5 top-3.5 h-4 w-4 text-slate-600 dark:text-slate-300" />
                <input
                  id="composio-api-key-settings"
                  className="provider-api-key-input w-full rounded-xl border border-slate-400 bg-white py-3 pr-4 pl-10 font-mono text-xs text-slate-900 shadow-sm placeholder:text-slate-500 transition-all focus:border-primary focus:ring-2 focus:ring-primary/20 focus:outline-none dark:border-slate-500 dark:bg-[#131b15] dark:text-slate-100 dark:placeholder:text-slate-400"
                  type="password"
                  required
                  value={apiKeyInput}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                  placeholder="Paste your key here (e.g. comp_...)"
                />
              </div>
              <div className="flex items-center justify-between mt-2.5 text-xs">
                <span className="text-muted-foreground text-[11px]">
                  Stored securely in local secrets file.
                </span>
                <a
                  href="https://app.composio.dev"
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex items-center gap-1 text-primary hover:text-primary/80 font-medium underline decoration-primary/30 transition-colors"
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
                className="theme-chip group hover-yellow inline-flex items-center gap-2 rounded-xl bg-primary hover:bg-primary/90 text-slate-950 px-5 py-2.5 text-xs font-bold tracking-wider uppercase transition-all shadow-[0_0_15px_rgba(250,204,21,0.25)] disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {saving ? (
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
              {configured && (
                <button
                  type="button"
                  onClick={() => {
                    setShowInput(false);
                    setApiKeyInput("");
                  }}
                  className="rounded-xl border border-glass-border px-4 py-2.5 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-surface-1 transition-all"
                >
                  Cancel
                </button>
              )}
            </div>
          </form>
        )}
      </div>

      {/* Connected Servers & DeepSpace Runtime Overview: Provider Native Grid */}
      <div className="theme-card relative rounded-[1.25rem] border border-glass-border bg-surface-0 p-6 space-y-5 shadow-sm">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-muted-foreground font-mono text-[9px] font-bold tracking-[0.24em] uppercase">
              DeepSpace Tool Engine
            </p>
            <div className="flex items-center gap-3 mt-1">
              <Server className="h-5 w-5 text-primary" />
              <h3 className="text-foreground text-lg font-bold tracking-tight">
                Connected MCP Servers & Tools
              </h3>
              <span className="theme-pill opacity-80">{servers.length} active</span>
            </div>
            <p className="text-muted-foreground text-xs mt-1">
              Servers currently provisioned and active in DeepSpace for autonomous tool calling.
            </p>
          </div>
          <Link
            href="/dashboard/mcp"
            className="theme-chip group hover-yellow inline-flex items-center gap-2 rounded-xl border border-glass-border bg-surface-1 px-4 py-2 text-xs font-bold tracking-wider uppercase text-foreground hover:border-primary/40 hover:bg-surface-2 transition-all self-start sm:self-auto"
          >
            <Layers className="h-3.5 w-3.5" />
            Manage in Marketplace
          </Link>
        </div>

        {servers.length === 0 ? (
          <div className="rounded-2xl border border-glass-border border-dashed bg-surface-0/50 p-8 text-center space-y-3">
            <p className="text-muted-foreground text-sm font-medium">No MCP servers connected yet</p>
            <p className="text-muted-foreground/70 text-xs max-w-md mx-auto">
              Once you connect apps from the MCP Marketplace or add custom stdio / HTTP endpoints, they will appear here with live tool permissions.
            </p>
            <div className="pt-2">
              <Link
                href="/dashboard/mcp"
                className="theme-chip group hover-yellow inline-flex items-center gap-2 rounded-xl bg-primary text-slate-950 px-5 py-2.5 text-xs font-bold tracking-wider uppercase transition-all shadow-[0_0_15px_rgba(250,204,21,0.2)]"
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
                  className="theme-card group hover-yellow relative flex flex-col justify-between rounded-[1.25rem] border border-glass-border bg-surface-0 p-5 transition-all duration-300 hover:border-primary/40 hover:shadow-[0_4px_24px_-8px_hsl(var(--primary)/0.18)]"
                >
                  {/* Top Row: Squircle Logo + Status Badge & Toggle Switch */}
                  <div>
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl border border-glass-border bg-surface-1/80 p-2 shadow-sm">
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
                            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                          )}
                          {s.status.replace("_", " ")}
                        </span>

                        {/* Live Toggle Switch */}
                        <MCPToggleSwitch
                          size="sm"
                          checked={isEnabled}
                          disabled={togglingId === s.id}
                          onChange={(next) => void handleToggleServer(s.id, next)}
                          title={isEnabled ? "Disable server in DeepSpace" : "Enable server in DeepSpace"}
                        />
                      </div>
                    </div>

                    {/* Middle: Title, Transport Tag, Tools Count */}
                    <div className="mt-3.5 space-y-1.5">
                      <p className="text-muted-foreground font-mono text-[9px] font-bold tracking-[0.2em] uppercase">
                        {s.transport} · {s.config?.oauth_mode === "none" ? "Local" : "OAuth"}
                      </p>
                      <h4 className="text-foreground truncate text-base font-bold tracking-tight" title={s.name}>
                        {s.name}
                      </h4>
                      <div className="pt-1">
                        <span className="theme-chip inline-flex items-center gap-1.5 rounded-lg border border-glass-border bg-surface-1/70 px-2.5 py-1 text-[11px] font-mono text-muted-foreground">
                          <span className="text-primary font-bold">⚡</span>
                          <span>{toolCount} tools active in DeepSpace</span>
                        </span>
                      </div>
                    </div>
                  </div>

                  {/* Footer: Configure Policy Link & Action */}
                  <div className="mt-4 pt-3 border-t border-glass-border flex items-center justify-between">
                    <Link
                      href={`/dashboard/mcp/inspector/${encodeURIComponent(s.id)}`}
                      className="theme-chip hover:text-primary inline-flex items-center gap-1.5 text-[11px] font-bold tracking-wider uppercase transition-colors"
                    >
                      <span>Configure Policy</span>
                      <ArrowRight className="h-3 w-3" />
                    </Link>

                    <Link
                      href={`/dashboard/mcp`}
                      className="text-muted-foreground hover:text-foreground text-[10px] font-mono uppercase tracking-widest transition-colors"
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

"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, CheckCircle, Key, Sparkles, Terminal } from "lucide-react";

import {
  disconnectMCPServerOAuth,
  getMarketplaceEntry,
  getMCPInspector,
  getMCPPolicy,
  getMCPTools,
  refreshMCPServer,
  saveMCPDirectToken,
  startComposioConnect,
  startMCPServerOAuth,
  type MCPConnectionPolicy,
  type MCPInspector as MCPInspectorData,
  type MCPMarketplaceEntry,
  type MCPTool,
} from "@/lib/mcp-api";
import { useRealtimeEvents } from "@/lib/realtime";
import { averqelConfirm } from "@/app/components/ui/AverQelDialogHost";

import MCPConnectionPolicyPanel from "../../_components/MCPConnectionPolicyPanel";
import MCPHealthStatus from "../../_components/MCPHealthStatus";
import MCPToolPermissionTable from "../../_components/MCPToolPermissionTable";

export default function MCPInspectorClient() {
  const routeParams = useParams();
  const router = useRouter();
  const id =
    typeof routeParams?.id === "string"
      ? routeParams.id
      : Array.isArray(routeParams?.id)
        ? routeParams.id[0]
        : "";
  const searchParams = useSearchParams();
  const [data, setData] = useState<MCPInspectorData | null>(null);
  const [policy, setPolicy] = useState<MCPConnectionPolicy | null>(null);
  const [tools, setTools] = useState<MCPTool[]>([]);
  const [provider, setProvider] = useState<MCPMarketplaceEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [callbackMessage, setCallbackMessage] = useState<string | null>(null);
  const mcpStatusParam = searchParams.get("mcp_status");

  const load = useCallback(async () => {
    if (!id) return;
    try {
      const [inspector, connectionPolicy, catalog] = await Promise.all([
        getMCPInspector(id),
        getMCPPolicy(id),
        getMCPTools(id),
      ]);
      const providerEntry = inspector.server.registry_entry_id
        ? await getMarketplaceEntry(inspector.server.registry_entry_id).catch(() => null)
        : null;
      setData(inspector);
      setPolicy(connectionPolicy);
      setTools(catalog.tools || []);
      setProvider(providerEntry);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Inspector unavailable");
    }
  }, [id]);

  useRealtimeEvents(
    (event) => {
      if (!id || event.data?.server_id === id) void load();
    },
    ["mcp"],
  );

  useEffect(() => {
    if (!id || typeof window === "undefined") return;
    queueMicrotask(() => void load());
  }, [id, load]);
  useEffect(() => {
    if (mcpStatusParam === "connected") {
      queueMicrotask(() =>
        setCallbackMessage(
          "Connected successfully. Your account, tools, and permissions are ready to configure.",
        ),
      );
      const url = new URL(window.location.href);
      url.searchParams.delete("mcp_status");
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    }
  }, [mcpStatusParam]);

  const refresh = async () => {
    setBusy("refresh");
    try {
      await refreshMCPServer(id);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to refresh catalog.");
    } finally {
      setBusy(null);
    }
  };
  const reconnect = async () => {
    setBusy("reconnect");
    try {
      const result = await startMCPServerOAuth(id);
      window.location.assign(result.authorization_url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to start reconnect.");
      setBusy(null);
    }
  };
  const [tokenInput, setTokenInput] = useState("");
  const [tokenSuccess, setTokenSuccess] = useState(false);
  const [composioKeyInput, setComposioKeyInput] = useState("");

  const disconnect = async () => {
    if (!(await averqelConfirm("Disconnect this account from AverQel?"))) return;
    setBusy("disconnect");
    try {
      await disconnectMCPServerOAuth(id);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to disconnect account.");
    } finally {
      setBusy(null);
    }
  };

  const handleSaveToken = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!tokenInput.trim()) return;
    setBusy("token");
    setError(null);
    try {
      await saveMCPDirectToken(id, tokenInput.trim());
      setTokenSuccess(true);
      setTokenInput("");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to save token.");
    } finally {
      setBusy(null);
    }
  };

  const handleConnectComposio = async () => {
    setBusy("composio");
    setError(null);
    try {
      const result = await startComposioConnect(id, composioKeyInput.trim() || undefined);
      if (result.authorization_url) {
        window.location.assign(result.authorization_url);
        return;
      }
      // No link means Composio already holds an active connection for this app.
      await load();
      setBusy(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to start Composio 1-click login.");
      setBusy(null);
    }
  };

  if (error && !data)
    return (
      <main className="mcp-theme-scope mx-auto max-w-5xl p-6">
        <p
          className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm font-medium text-red-600 dark:text-red-400"
          role="alert"
        >
          {error}
        </p>
      </main>
    );
  if (!data || !policy)
    return (
      <main className="mcp-theme-scope mx-auto max-w-5xl p-6 text-sm text-muted-foreground">
        Loading MCP connection…
      </main>
    );
  const server = data.server;
  const identity = server.account_identity || {};
  const config = server.config || {};

  return (
    <main className="mcp-theme-scope mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <button
            type="button"
            onClick={() => {
              if (window.history.length > 1) router.back();
              else router.push("/dashboard/mcp");
            }}
            className="mb-3 inline-flex items-center gap-2 rounded-xl border border-glass-border bg-surface-1 px-3.5 py-1.5 text-xs font-semibold text-foreground transition hover:border-primary/40 hover:bg-surface-2 shadow-sm"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            Back
          </button>
          <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">MCP connection</p>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">{server.name}</h1>
          <p className="mt-1 text-sm font-medium text-muted-foreground">
            {server.provider_slug || "Approved remote provider"} ·{" "}
            {server.transport.replaceAll("_", " ")}
          </p>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1 text-xs font-bold uppercase tracking-wider shadow-sm ${
            server.status === "connected"
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 shadow-[0_0_12px_rgba(16,185,129,0.15)]"
              : "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400"
          }`}
        >
          <span
            className={`h-2 w-2 rounded-full ${
              server.status === "connected" ? "bg-emerald-500 animate-pulse" : "bg-amber-500"
            }`}
          />
          {server.status}
        </span>
      </div>
      {error && (
        <p
          className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3.5 text-sm font-medium text-amber-700 dark:text-amber-300"
          role="alert"
        >
          {error}
        </p>
      )}
      {callbackMessage && (
        <p
          className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-3.5 text-sm font-medium text-emerald-700 dark:text-emerald-300"
          role="status"
        >
          {callbackMessage}
        </p>
      )}
      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric
          label="Connection"
          value={server.enabled && server.status === "connected" ? "Enabled" : "Not active"}
        />
        <Metric label="Catalog revision" value={String(server.catalog_revision || 0)} />
        <Metric label="Tools" value={String(tools.length)} />
        <Metric label="Last refresh" value={formatDate(data.diagnostics.last_catalog_sync_at)} />
      </section>
      <section className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-glass-border bg-surface-0/95 dark:bg-surface-1/40 p-5 sm:p-6 shadow-sm">
        <div>
          <h2 className="text-xs font-bold tracking-wider text-muted-foreground uppercase">Health</h2>
          <div className="mt-3">
            <MCPHealthStatus
              health={{
                status: server.status === "connected" ? "healthy" : server.status,
                last_checked_at: data.diagnostics.last_catalog_sync_at,
                detail: data.diagnostics.last_error,
              }}
            />
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={busy !== null}
            className="rounded-xl border border-glass-border bg-surface-1 px-4 py-2 text-xs font-bold text-foreground hover:bg-surface-2 transition-all shadow-sm disabled:opacity-40"
          >
            {busy === "refresh" ? "Refreshing…" : "Refresh catalog"}
          </button>
          <button
            type="button"
            onClick={() => void reconnect()}
            disabled={busy !== null}
            className="rounded-xl border border-cyan-500/30 bg-cyan-500/10 dark:bg-cyan-500/20 px-4 py-2 text-xs font-bold text-cyan-700 dark:text-cyan-300 hover:bg-cyan-500/20 dark:hover:bg-cyan-500/30 transition-all shadow-sm disabled:opacity-40"
          >
            {busy === "reconnect" ? "Opening…" : "Reconnect"}
          </button>
          <button
            type="button"
            onClick={() => void disconnect()}
            disabled={busy !== null}
            className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-2 text-xs font-bold text-red-600 dark:text-red-400 hover:bg-red-500/20 transition-all shadow-sm disabled:opacity-40"
          >
            Disconnect
          </button>
        </div>
      </section>

      {server.transport === "stdio" ? (
        <section className="rounded-2xl border border-emerald-500/30 bg-emerald-500/5 dark:bg-emerald-950/20 p-5 sm:p-6 shadow-sm">
          <div className="flex items-center gap-2.5 text-emerald-700 dark:text-emerald-400">
            <Terminal className="h-5 w-5" />
            <h2 className="text-base font-bold text-foreground">Local STDIO MCP Runner Active</h2>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            Zero login needed. Spawns directly on your machine via stdio (npx/python). Offline, high-speed, and completely private.
          </p>
        </section>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {/* Composio 1-Click Browser Login */}
          <section className="mcp-card-cyan flex flex-col justify-between rounded-2xl border border-cyan-500/30 bg-cyan-500/[0.04] dark:bg-cyan-950/20 p-5 sm:p-6 shadow-sm">
            <div>
              <div className="flex items-center gap-2.5">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-cyan-500/10 text-cyan-600 dark:text-cyan-400">
                  <Sparkles className="h-5 w-5" />
                </div>
                <h2 className="text-base font-bold text-foreground">1-Click Cloud Login (Composio)</h2>
              </div>
              <p className="mt-2.5 text-xs leading-relaxed text-muted-foreground">
                Connect Google (Gmail, Drive, Calendar), Notion, Slack, GitHub, Linear, Jira (250+ apps) instantly. Zero Google Cloud Console or developer portal setup required.
              </p>
            </div>
            <div className="mt-5 space-y-3">
              <input
                type="password"
                placeholder="Composio API Key (optional if set in env)..."
                value={composioKeyInput}
                onChange={(e) => setComposioKeyInput(e.target.value)}
                className="w-full rounded-xl border border-glass-border bg-surface-0 dark:bg-surface-1/90 px-3.5 py-2.5 text-sm text-foreground placeholder:text-muted-foreground/60 focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/20 focus:outline-none transition-all shadow-sm"
              />
              <button
                type="button"
                onClick={() => void handleConnectComposio()}
                disabled={busy !== null}
                className="mcp-btn-cyan flex w-full items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-bold text-white transition-all"
              >
                {busy === "composio" ? "Opening Browser..." : "1-Click Cloud Connect"}
              </button>
              <p className="text-[11px] text-muted-foreground">
                Get free key at{" "}
                <a
                  href="https://app.composio.dev"
                  target="_blank"
                  rel="noreferrer"
                  className="font-semibold text-cyan-600 dark:text-cyan-400 underline hover:text-cyan-700 dark:hover:text-cyan-300"
                >
                  app.composio.dev
                </a>
                , or use Direct Token Bypass →
              </p>
            </div>
          </section>

          {/* Direct Token / API Key Bypass */}
          <section className="mcp-card-purple flex flex-col justify-between rounded-2xl border border-purple-500/30 bg-purple-500/[0.04] dark:bg-purple-950/20 p-5 sm:p-6 shadow-sm">
            <div>
              <div className="flex items-center gap-2.5">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-purple-500/10 text-purple-600 dark:text-purple-400">
                  <Key className="h-5 w-5" />
                </div>
                <h2 className="text-base font-bold text-foreground">Direct Token / API Key Bypass</h2>
              </div>
              <p className="mt-2.5 text-xs leading-relaxed text-muted-foreground">
                Paste your Personal Access Token or API Key (e.g. Notion{" "}
                <code className="rounded-md border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.5 font-mono text-[11px] font-bold text-purple-700 dark:text-purple-300">
                  secret_...
                </code>
                , GitHub{" "}
                <code className="rounded-md border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.5 font-mono text-[11px] font-bold text-purple-700 dark:text-purple-300">
                  ghp_...
                </code>
                , Slack{" "}
                <code className="rounded-md border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.5 font-mono text-[11px] font-bold text-purple-700 dark:text-purple-300">
                  xoxb-...
                </code>
                ) to connect immediately without OAuth.
              </p>
            </div>
            <form onSubmit={handleSaveToken} className="mt-5 space-y-3">
              <input
                type="password"
                placeholder="Paste token or API key here..."
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                className="w-full rounded-xl border border-glass-border bg-surface-0 dark:bg-surface-1/90 px-3.5 py-2.5 text-sm text-foreground placeholder:text-muted-foreground/60 focus:border-purple-500 focus:ring-2 focus:ring-purple-500/20 focus:outline-none transition-all shadow-sm"
              />
              <button
                type="submit"
                disabled={busy !== null || !tokenInput.trim()}
                className="mcp-btn-purple flex w-full items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-bold text-white transition-all"
              >
                {busy === "token" ? "Saving..." : "Save Token & Connect"}
              </button>
              {tokenSuccess && (
                <p className="flex items-center gap-1.5 text-xs font-semibold text-emerald-600 dark:text-emerald-400">
                  <CheckCircle className="h-3.5 w-3.5" /> Token encrypted and connected successfully!
                </p>
              )}
            </form>
          </section>
        </div>
      )}

      {(identity.email || identity.display_name) && (
        <section className="rounded-2xl border border-glass-border bg-surface-0/95 dark:bg-surface-1/40 p-5 sm:p-6 shadow-sm">
          <h2 className="text-base font-bold text-foreground">Connected account</h2>
          <p className="mt-2 text-sm font-semibold text-foreground">{identity.display_name || identity.email}</p>
          {identity.display_name && identity.email && (
            <p className="mt-0.5 text-xs text-muted-foreground">{identity.email}</p>
          )}
        </section>
      )}
      <MCPConnectionPolicyPanel
        serverId={server.id}
        policy={policy}
        onSaved={(saved) => {
          setPolicy(saved);
          setTools((current) =>
            current.map((tool) => ({
              ...tool,
              mode: saved.tool_modes[tool.name] || saved.default_tool_mode,
            })),
          );
        }}
      />
      <MCPToolPermissionTable serverId={server.id} tools={tools} onChanged={setTools} />
      <section className="rounded-2xl border border-glass-border bg-surface-0/95 dark:bg-surface-1/40 p-5 sm:p-6 shadow-sm">
        <h2 className="text-base font-bold text-foreground">OAuth and catalog status</h2>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
          <Metric
            label="Authentication"
            value={
              config.oauth_mode === "none"
                ? "Anonymous"
                : data.diagnostics.oauth_configured
                  ? "OAuth connected"
                  : "OAuth required"
            }
          />
          <Metric
            label="Requested OAuth scopes"
            value={
              provider?.requested_scopes.length
                ? provider.requested_scopes.join(", ")
                : "Managed by the provider authorization screen"
            }
          />
          <Metric
            label="Granted OAuth scopes"
            value={
              server.granted_scopes?.length ? server.granted_scopes.join(", ") : "Not yet confirmed"
            }
          />
          <Metric label="Reconnect attempts" value={String(data.diagnostics.reconnect_attempts)} />
          <Metric
            label="Current tool catalog"
            value={`${tools.length} tools at revision ${server.catalog_revision || 0}`}
          />
        </dl>
        {provider?.scope_note && (
          <p className="mt-3 text-xs leading-5 text-muted-foreground">{provider.scope_note}</p>
        )}
        <p className="mt-4 text-xs leading-5 text-muted-foreground">
          OAuth tokens, refresh tokens, client secrets, and raw OAuth metadata are never returned to
          this page. Only verified scope names, safe account labels, and catalog status are shown.
        </p>
      </section>
      <section className="mcp-safe-events-panel rounded-2xl border border-glass-border bg-surface-0/95 dark:bg-surface-1/40 p-5 sm:p-6 shadow-sm">
        <h2 className="mb-3 text-base font-bold text-foreground">Recent safe events</h2>
        <div className="mcp-safe-events-list max-h-80 space-y-2 overflow-y-auto overscroll-contain pr-2 [scrollbar-width:thin]">
          {data.events.length === 0 ? (
            <p className="text-sm text-muted-foreground">No lifecycle events recorded.</p>
          ) : (
            data.events.map((event) => (
              <div
                key={`${event.sequence}-${event.event_type}`}
                className="flex flex-wrap justify-between gap-2 border-b border-glass-border py-2 text-sm"
              >
                <span className="font-mono text-xs font-semibold text-foreground">{event.event_type}</span>
                <span className="text-xs text-muted-foreground">
                  #{event.sequence} · {formatDateTime(event.created_at)}
                </span>
              </div>
            ))
          )}
        </div>
      </section>
    </main>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-glass-border bg-surface-0/95 dark:bg-surface-1/60 p-4 shadow-sm transition-all hover:border-glass-border/80">
      <p className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">{label}</p>
      <p className="mt-1 text-base font-bold text-foreground">{value}</p>
    </div>
  );
}
function formatDate(value?: string | null): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unavailable"
    : new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeZone: "UTC" }).format(date);
}
function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unavailable"
    : new Intl.DateTimeFormat("en-GB", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "UTC",
      }).format(date);
}

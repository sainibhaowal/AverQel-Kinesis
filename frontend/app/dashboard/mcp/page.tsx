"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { useSearchParams } from "next/navigation";
import { useRouter } from "next/navigation";
import {
  PlugZap,
  Search,
  Plus,
  Globe,
  Key,
  Terminal,
  X,
  CheckCircle2,
  AlertCircle,
  Loader2,
  Cpu,
  Server,
  Sparkles,
  Copy,
  Check,
  RefreshCw,
  Trash2,
  ExternalLink,
} from "lucide-react";

import {
  connectMarketplaceEntry,
  createCustomMCPServer,
  deleteMCPServer,
  getMarketplace,
  getMarketplaceFacets,
  listMCPServers,
  refreshMCPServer,
  startComposioConnect,
  startMCPServerOAuth,
  toggleMCPServer,
  type MCPConnection,
  type MCPMarketplaceFacets,
  type MCPMarketplaceEntry,
  type MarketplaceQuery,
} from "@/lib/mcp-api";

import MCPMarketplaceCard from "./_components/MCPMarketplaceCard";
import MCPHealthStatus from "./_components/MCPHealthStatus";
import MCPFilterSelect from "./_components/MCPFilterSelect";
import MCPToggleSwitch from "./_components/MCPToggleSwitch";
import MCPDetailDialog from "./_components/MCPDetailDialog";
import ComposioConnectWarningModal from "./_components/ComposioConnectWarningModal";
import toast from "react-hot-toast";
import { readMCPActiveContext } from "@/lib/mcp-context";
import DashboardSectionHeader from "@/app/components/ui/DashboardSectionHeader";
import { useRealtimeEvents } from "@/lib/realtime";
import { averqelConfirm } from "@/app/components/ui/AverQelDialogHost";

const FALLBACK_CATEGORIES = [
  "Productivity",
  "Development",
  "Communication",
  "Files",
  "Knowledge",
  "Finance",
  "Marketing",
];

export type MCPCategoryView = "all" | "remote" | "local" | "custom" | "installed";

export interface PresetTemplate {
  id: string;
  name: string;
  badge: string;
  transport: "stdio" | "sse" | "streamable_http";
  command?: string;
  args?: string;
  serverUrl?: string;
  description: string;
  icon: string;
  highlights: string[];
}

const PRESET_TEMPLATES: PresetTemplate[] = [
  {
    id: "postgres",
    name: "PostgreSQL Database",
    badge: "Local Stdio",
    transport: "stdio",
    command: "npx",
    args: "-y @modelcontextprotocol/server-postgres",
    serverUrl: "",
    description: "Custom stdio command: npx -y @modelcontextprotocol/server-postgres",
    icon: "🐘",
    highlights: ["Table introspection", "SQL queries", "Schema analysis"],
  },
  {
    id: "local-sse",
    name: "Local SSE Server",
    badge: "Custom SSE",
    transport: "sse",
    command: "",
    args: "",
    serverUrl: "http://localhost:8000/sse",
    description: "Custom SSE/HTTP endpoint: http://localhost:8000/sse",
    icon: "⚡",
    highlights: ["Realtime SSE events", "Port 8000 endpoint", "Direct tools"],
  },
  {
    id: "filesystem",
    name: "Local Filesystem",
    badge: "Local Stdio",
    transport: "stdio",
    command: "npx",
    args: "-y @modelcontextprotocol/server-filesystem .",
    serverUrl: "",
    description: "Custom stdio command: npx -y @modelcontextprotocol/server-filesystem",
    icon: "📁",
    highlights: ["Read / write files", "Directory listing", "Project sandboxing"],
  },
  {
    id: "remote-http",
    name: "Remote Streamable HTTP",
    badge: "Streamable HTTP",
    transport: "streamable_http",
    command: "",
    args: "",
    serverUrl: "http://localhost:3000/mcp",
    description: "Custom HTTP POST endpoint for streamable MCP server",
    icon: "🌐",
    highlights: ["Streamable JSON-RPC", "Custom API auth", "Cloud endpoint"],
  },
];

export default function MCPDashboard() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const [tab, setTab] = useState<MCPCategoryView>("all");
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [transport, setTransport] = useState("");
  const [official, setOfficial] = useState<boolean | null>(null);
  const [verified, setVerified] = useState<boolean | null>(null);
  const [authType, setAuthType] = useState("");
  const [trustStatus, setTrustStatus] = useState("");
  const [sort, setSort] = useState<NonNullable<MarketplaceQuery["sort"]>>("default");
  const [facets, setFacets] = useState<MCPMarketplaceFacets>({
    categories: [],
    transports: [],
    auth_types: [],
    trust_statuses: [],
  });
  const [items, setItems] = useState<MCPMarketplaceEntry[]>([]);
  const [installed, setInstalled] = useState<MCPConnection[]>([]);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [clientReady, setClientReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [callbackMessage, setCallbackMessage] = useState<string | null>(null);
  const [showAddModal, setShowAddModal] = useState(false);
  const [modalInitialPreset, setModalInitialPreset] = useState<PresetTemplate | null>(null);
  const [selectedDetailEntry, setSelectedDetailEntry] = useState<MCPMarketplaceEntry | null>(null);
  const [connectingEntryId, setConnectingEntryId] = useState<string | null>(null);
  const [composioWarning, setComposioWarning] = useState<{
    entry: MCPMarketplaceEntry;
    server?: MCPConnection | null;
    message?: string | null;
  } | null>(null);

  // Compute transport parameter based on current tab
  const effectiveTransport = useMemo(() => {
    if (tab === "local") return "stdio";
    return transport;
  }, [tab, transport]);

  const query = useMemo(
    () => ({
      q,
      category,
      transport: effectiveTransport,
      official,
      verified,
      authType,
      trustStatus,
      sort,
      page,
    }),
    [q, category, effectiveTransport, official, verified, authType, trustStatus, sort, page],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [marketplace, servers, facetData] = await Promise.all([
        getMarketplace(query),
        listMCPServers(),
        getMarketplaceFacets().catch(() => null),
      ]);
      setItems(marketplace.items || []);
      setTotal(marketplace.total || 0);
      setPages(Math.max(1, marketplace.pages || 1));
      setInstalled(servers || []);
      if (facetData) setFacets(facetData);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "MCP marketplace unavailable");
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  useRealtimeEvents(() => {
    void load();
  }, ["mcp"]);

  useEffect(() => {
    queueMicrotask(() => setClientReady(true));
  }, []);

  useEffect(() => {
    const status = searchParams.get("mcp_status");
    const serverId = searchParams.get("server_id");
    if (!status) return;
    if (status === "connected" && serverId) {
      const context = readMCPActiveContext();
      const target = new URLSearchParams({ mcp_status: "connected" });
      if (context?.conversation_id) target.set("conversation_id", context.conversation_id);
      if (context?.deepspace_id) target.set("deepspace_id", context.deepspace_id);
      router.replace(
        `/dashboard/mcp/inspector/${encodeURIComponent(serverId)}?${target.toString()}`,
      );
      return;
    }
    queueMicrotask(() => {
      setTab("all");
      setCallbackMessage("MCP authorization did not complete.");
    });
    const url = new URL(window.location.href);
    url.searchParams.delete("mcp_status");
    url.searchParams.delete("server_id");
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  }, [router, searchParams]);

  const connect = async (entry: MCPMarketplaceEntry) => {
    setConnectingEntryId(entry.id);
    setError(null);
    try {
      const result = await connectMarketplaceEntry(entry.id);
      if (result.authorization_url) {
        toast.success(`Redirecting to ${entry.name} authorization...`);
        window.location.assign(result.authorization_url);
        return;
      }
      if (result.setup_required) {
        setComposioWarning({
          entry,
          server: result.server,
          message: result.message,
        });
        toast(result.message || "Composio API key required for 1-click connect", {
          icon: "⚠️",
        });
        await load();
        return;
      }
      if (result.server?.id) {
        toast.success(`${entry.name} connection established.`);
        router.push(`/dashboard/mcp/inspector/${encodeURIComponent(result.server.id)}`);
      } else {
        setTab("installed");
        await load();
      }
    } catch (reason) {
      const msg = reason instanceof Error ? reason.message : `Unable to connect ${entry.name}`;
      setError(msg);
      toast.error(msg);
    } finally {
      setConnectingEntryId(null);
    }
  };

  const reconnect = async (server: MCPConnection) => {
    setConnectingEntryId(server.id);
    setError(null);
    try {
      // Composio-brokered apps authorize through Composio; every other remote
      // app uses the server's own OAuth provider profile.
      if ((server.provider_slug ?? "").startsWith("composio:")) {
        const result = await startComposioConnect(server.id);
        if (result.authorization_url) {
          window.location.assign(result.authorization_url);
          return;
        }
        toast.success(`${server.name} is already connected.`);
        await load();
        return;
      }
      const result = await startMCPServerOAuth(server.id);
      if (result.authorization_url) {
        window.location.assign(result.authorization_url);
      }
    } catch (reason) {
      const msg = reason instanceof Error ? reason.message : "Unable to reconnect provider.";
      setError(msg);
      toast.error(msg);
    } finally {
      setConnectingEntryId(null);
    }
  };

  const handleToggle = async (server: MCPConnection) => {
    const updated = !server.enabled;
    // Optimistic UI toggle
    setInstalled((prev) =>
      prev.map((s) => (s.id === server.id ? { ...s, enabled: updated } : s)),
    );
    try {
      await toggleMCPServer(server.id);
    } catch (reason) {
      // Rollback on error
      setInstalled((prev) =>
        prev.map((s) => (s.id === server.id ? { ...s, enabled: server.enabled } : s)),
      );
      setError(reason instanceof Error ? reason.message : "Failed to toggle server state");
    }
  };

  const clearFilters = () => {
    setPage(1);
    setCategory("");
    setTransport("");
    setOfficial(null);
    setVerified(null);
    setAuthType("");
    setTrustStatus("");
    setSort("default");
  };

  const openPresetModal = (preset: PresetTemplate) => {
    setModalInitialPreset(preset);
    setShowAddModal(true);
  };

  const openBlankModal = () => {
    setModalInitialPreset(null);
    setShowAddModal(true);
  };

  // Filter items based on active category view
  const displayItems = useMemo(() => {
    if (tab === "remote") {
      return items.filter((entry) => entry.transport !== "stdio");
    }
    if (tab === "local") {
      return items.filter((entry) => entry.transport === "stdio");
    }
    return items;
  }, [items, tab]);

  // Filter user-added custom servers
  const customServers = useMemo(() => {
    return installed.filter(
      (server) =>
        server.provider_slug?.startsWith("custom-") ||
        server.config?.source === "custom-remote" ||
        !server.registry_entry_id,
    );
  }, [installed]);

  const categoryTabs = useMemo(
    () => [
      {
        id: "all" as const,
        label: "All Connectors",
        icon: PlugZap,
      },
      {
        id: "remote" as const,
        label: "Remote MCP",
        icon: Globe,
      },
      {
        id: "local" as const,
        label: "Local MCP (Stdio)",
        icon: Cpu,
      },
      {
        id: "custom" as const,
        label: "Self-Added / Custom",
        icon: Sparkles,
        badge: customServers.length > 0 ? customServers.length : undefined,
      },
      {
        id: "installed" as const,
        label: "Installed",
        icon: Server,
        badge: installed.length,
      },
    ],
    [customServers.length, installed.length],
  );

  const categories = facets.categories.length ? facets.categories : FALLBACK_CATEGORIES;

  return (
    <main className="mcp-theme-scope w-full min-w-0 space-y-7 pb-12">
      <header className="space-y-6">
        <DashboardSectionHeader
          title="MCP Marketplace"
          subtitle="APPROVED CONNECTORS, LOCAL TOOLS & CUSTOM SERVERS"
          icon={PlugZap}
          accentClassName="bg-emerald-500 text-emerald-400"
          accentGlowClassName="shadow-[0_0_18px_rgba(16,185,129,0.35)]"
          actions={
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={openBlankModal}
                className="flex items-center gap-1.5 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-bold px-4 py-2 text-xs shadow-xs hover:shadow-sm active:scale-95 border border-emerald-700/20 dark:bg-emerald-500 dark:hover:bg-emerald-400 dark:text-slate-950 dark:font-extrabold dark:shadow-[0_0_15px_rgba(16,185,129,0.3)] transition-all"
              >
                <Plus className="h-4 w-4" /> Add Custom Server
              </button>
            </div>
          }
        />

        {/* Category Navigation Bar */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-glass-border pb-3 dark:border-white/10">
          <div
            aria-label="MCP connector categories"
            className="relative flex flex-wrap items-center gap-1 rounded-2xl border border-slate-300/80 bg-slate-100 p-1 shadow-inner dark:border-white/10 dark:bg-slate-950/80"
          >
            {categoryTabs.map((item) => {
              const isSelected = tab === item.id;
              const Icon = item.icon;
              return (
                <button
                  key={item.id}
                  type="button"
                  aria-pressed={isSelected}
                  onClick={() => {
                    setTab(item.id);
                    setPage(1);
                  }}
                  className={`mcp-category-tab-btn relative flex items-center gap-2 rounded-xl px-3.5 py-2 text-xs transition-colors duration-200 outline-none select-none ${
                    isSelected
                      ? "text-emerald-950 dark:text-emerald-100 font-bold"
                      : "text-slate-700 hover:text-slate-950 dark:text-white/70 dark:hover:text-white font-semibold"
                  }`}
                >
                  {isSelected && (
                    <motion.div
                      layoutId="mcp-category-slider-pill"
                      className="absolute inset-0 rounded-xl bg-white border border-slate-300/90 shadow-sm dark:bg-emerald-500/20 dark:border-emerald-400/35 dark:shadow-[0_0_15px_rgba(16,185,129,0.25)]"
                      transition={{ type: "spring", stiffness: 450, damping: 32 }}
                    />
                  )}
                  <span className="relative z-10 flex items-center gap-2">
                    <Icon
                      className={`h-3.5 w-3.5 transition-colors duration-200 ${
                        isSelected
                          ? "text-emerald-600 dark:text-emerald-400"
                          : "text-slate-500 dark:text-white/50 group-hover:text-slate-700 dark:group-hover:text-white/80"
                      }`}
                    />
                    <span>{item.label}</span>
                    {item.badge !== undefined && (
                      <span
                        className={`rounded-full px-2 py-0.5 text-[10px] font-extrabold transition-colors duration-200 ${
                          isSelected
                            ? "bg-emerald-600 text-white shadow-2xs dark:bg-emerald-400/30 dark:text-emerald-200 dark:border dark:border-emerald-400/40"
                            : "bg-slate-300 text-slate-800 dark:bg-white/15 dark:text-white/80"
                        }`}
                      >
                        ({item.badge})
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-slate-500 dark:text-white/50">
              {tab === "installed"
                ? `${installed.length} connections active`
                : tab === "custom"
                  ? `${customServers.length} custom servers`
                  : `${total.toLocaleString("en-US")} connectors available`}
            </span>
          </div>
        </div>

        {/* Filter bar (displayed on marketplace, remote, and local tabs) */}
        {tab !== "installed" && tab !== "custom" && (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2.5 items-center">
              <label className="mcp-marketplace-search flex-1 min-w-[240px]">
                <Search className="mcp-marketplace-search-icon" aria-hidden="true" />
                <input
                  className="mcp-marketplace-search-input"
                  aria-label="Search providers and tools"
                  placeholder="Search providers and tools..."
                  value={q}
                  onChange={(event) => {
                    setPage(1);
                    setQ(event.target.value);
                  }}
                />
              </label>

              {tab === "all" && (
                <MCPFilterSelect
                  label="All transports"
                  value={transport}
                  options={[
                    { value: "", label: "All transports" },
                    ...facets.transports.map((item) => ({
                      value: item,
                      label: transportLabel(item),
                    })),
                  ]}
                  onChange={(value) => {
                    setPage(1);
                    setTransport(value);
                  }}
                />
              )}

              <MCPFilterSelect
                label="Filter by"
                value={authType ? `auth:${authType}` : trustStatus ? `trust:${trustStatus}` : ""}
                options={[
                  { value: "", label: "Filter by" },
                  ...facets.auth_types.map((item) => ({ value: `auth:${item}`, label: item })),
                  ...facets.trust_statuses.map((item) => ({ value: `trust:${item}`, label: item })),
                ]}
                onChange={(value) => {
                  setPage(1);
                  setAuthType(value.startsWith("auth:") ? value.slice(5) : "");
                  setTrustStatus(value.startsWith("trust:") ? value.slice(6) : "");
                }}
              />

              <MCPFilterSelect
                label="Sort by"
                value={sort}
                options={[
                  { value: "default", label: "Sort by" },
                  { value: "popular", label: "Popular" },
                  { value: "trending", label: "Trending" },
                  { value: "new", label: "New" },
                  { value: "alphabetical", label: "Alphabetical" },
                ]}
                onChange={(value) => {
                  setPage(1);
                  setSort(value as NonNullable<MarketplaceQuery["sort"]>);
                }}
              />

              <Chip
                label="Verified"
                active={verified === true}
                onClick={() => {
                  setPage(1);
                  setVerified(verified === true ? null : true);
                }}
              />

              <Chip
                label="Official"
                active={official === true}
                onClick={() => {
                  setPage(1);
                  setOfficial(official === true ? null : true);
                }}
              />

              <button
                type="button"
                aria-label="More filters"
                className="flex items-center gap-1.5 rounded-xl border border-slate-300 hover:border-slate-400 bg-white hover:bg-slate-100 px-3.5 py-2 text-xs font-bold text-slate-800 shadow-2xs dark:border-white/15 dark:bg-slate-900/60 dark:text-slate-200 dark:hover:bg-white/10 transition-all"
                onClick={() => setFiltersOpen((open) => !open)}
              >
                <span>{filtersOpen ? "▾" : "▸"}</span> Categories
              </button>
            </div>

            {filtersOpen && (
              <div className="space-y-3 rounded-2xl border border-slate-200/90 bg-white p-4 shadow-sm dark:border-white/10 dark:bg-slate-900/40">
                <div className="flex flex-wrap gap-2">
                  <Chip
                    label="All"
                    active={!category}
                    onClick={() => {
                      setPage(1);
                      setCategory("");
                    }}
                  />
                  {categories.map((item) => (
                    <Chip
                      key={item}
                      label={item}
                      active={category === item}
                      onClick={() => {
                        setPage(1);
                        setCategory(item);
                      }}
                    />
                  ))}
                  <button
                    type="button"
                    className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:text-slate-950 hover:bg-slate-100 shadow-2xs dark:border-white/15 dark:bg-white/5 dark:text-slate-300 dark:hover:text-white dark:hover:bg-white/10 transition"
                    onClick={clearFilters}
                  >
                    Clear filters
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </header>

      {error && (
        <div
          className="flex items-center justify-between rounded-xl border border-red-500/30 bg-red-500/10 p-3.5 text-sm text-red-200"
          role="alert"
        >
          <span>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="text-red-300 hover:text-white"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {callbackMessage && (
        <div
          className="flex items-center justify-between rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-3.5 text-sm text-emerald-100"
          role="status"
        >
          <span>{callbackMessage}</span>
          <button
            type="button"
            onClick={() => setCallbackMessage(null)}
            className="text-emerald-300 hover:text-white"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* VIEW: Self-Added / Custom MCP Servers */}
      {tab === "custom" && (
        <section className="space-y-8">
          {/* Quick-add preset template strip */}
          <div className="rounded-2xl border border-slate-200/90 bg-white p-6 shadow-sm space-y-5 dark:border-white/10 dark:bg-slate-900/60 dark:shadow-xl">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="flex items-center gap-2.5">
                  <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-emerald-600 text-white dark:bg-emerald-500 dark:text-slate-950 shadow-xs font-bold">
                    <Sparkles className="h-4 w-4" />
                  </span>
                  <h2 className="text-lg font-bold text-slate-950 dark:text-white">
                    Add Any Custom MCP Server You Want
                  </h2>
                </div>
                <p className="mt-1 text-xs text-slate-600 dark:text-white/60">
                  Connect local stdio command-line tools or custom SSE/HTTP endpoints directly to
                  DeepSpace with 1-click presets or manual configuration.
                </p>
              </div>
              <button
                type="button"
                onClick={openBlankModal}
                className="flex items-center gap-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-bold px-4 py-2 text-xs shadow-xs hover:shadow-sm active:scale-95 dark:bg-emerald-500 dark:hover:bg-emerald-400 dark:text-slate-950 dark:font-extrabold dark:shadow-[0_0_15px_rgba(16,185,129,0.3)] transition-all"
              >
                <Plus className="h-4 w-4" /> Custom Configuration
              </button>
            </div>

            {/* Presets Grid */}
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {PRESET_TEMPLATES.map((preset) => (
                <div
                  key={preset.id}
                  className="group relative flex flex-col justify-between rounded-xl border border-slate-200 bg-slate-50/90 p-4 transition-all hover:border-emerald-500/60 hover:bg-white hover:shadow-md dark:border-white/10 dark:bg-white/[0.04] dark:hover:border-emerald-400/50 dark:hover:bg-white/[0.07] dark:hover:shadow-[0_4px_20px_rgba(0,0,0,0.4)]"
                >
                  <div className="space-y-2.5">
                    <div className="flex items-center justify-between">
                      <span className="text-2xl">{preset.icon}</span>
                      <span className="rounded-full border border-slate-300 bg-white px-2.5 py-0.5 text-[10px] font-mono font-bold text-slate-800 shadow-2xs dark:border-white/15 dark:bg-white/10 dark:text-slate-200">
                        {preset.badge}
                      </span>
                    </div>
                    <div>
                      <h3 className="text-sm font-bold text-slate-950 dark:text-white group-hover:text-emerald-600 dark:group-hover:text-emerald-300 transition-colors">
                        {preset.name}
                      </h3>
                      <p className="mt-1 text-[11px] font-mono text-slate-600 dark:text-white/50 line-clamp-2">
                        {preset.description}
                      </p>
                    </div>
                  </div>
                  <div className="mt-4 pt-3 border-t border-slate-200 dark:border-white/10">
                    <button
                      type="button"
                      onClick={() => openPresetModal(preset)}
                      className="w-full flex items-center justify-center gap-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-bold py-2 text-xs shadow-xs hover:shadow-sm active:scale-95 dark:bg-emerald-500 dark:hover:bg-emerald-400 dark:text-slate-950 dark:font-extrabold dark:shadow-[0_0_12px_rgba(16,185,129,0.25)] transition-all"
                    >
                      <Plus className="h-3.5 w-3.5" />
                      Use Template
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* User's custom installed servers list */}
          <div className="space-y-4">
            <h3 className="text-sm font-semibold text-slate-900 dark:text-white/80">
              Your Custom Added Servers ({customServers.length})
            </h3>

            {customServers.length === 0 ? (
              <div className="rounded-2xl border border-glass-border bg-surface-0/60 dark:border-white/10 dark:bg-white/[0.02] p-8 text-center space-y-3">
                <Terminal className="mx-auto h-8 w-8 text-slate-400 dark:text-white/30" />
                <p className="text-sm text-slate-600 dark:text-white/60">
                  No custom MCP servers added yet. Click one of the templates above or add your own!
                </p>
                <button
                  type="button"
                  onClick={openBlankModal}
                  className="inline-flex items-center gap-2 rounded-xl border border-glass-border bg-surface-1 hover:bg-surface-2 text-slate-800 dark:border-white/15 dark:bg-white/5 dark:hover:bg-white/10 dark:text-white px-4 py-2 text-xs font-semibold transition"
                >
                  <Plus className="h-4 w-4" /> Add Stdio or SSE Server
                </button>
              </div>
            ) : (
              <div className="grid gap-4 lg:grid-cols-2">
                {customServers.map((server) => (
                  <CustomServerCard
                    key={server.id}
                    server={server}
                    clientReady={clientReady}
                    onToggle={handleToggle}
                    onRefresh={async () => {
                      await refreshMCPServer(server.id);
                      await load();
                    }}
                    onDisconnect={async () => {
                      if (await averqelConfirm(`Disconnect custom server "${server.name}"?`)) {
                        await deleteMCPServer(server.id);
                        await load();
                      }
                    }}
                  />
                ))}
              </div>
            )}
          </div>
        </section>
      )}

      {/* VIEW: Installed Connections */}
      {tab === "installed" && (
        <section className="space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-xs text-white/50">
              Manage your active MCP connections and toggle their availability for DeepSpace models.
            </p>
            <button
              type="button"
              onClick={openBlankModal}
              className="flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 transition"
            >
              <Plus className="h-3.5 w-3.5" /> Add Custom Server
            </button>
          </div>

          {installed.length === 0 ? (
            <div className="rounded-2xl border border-white/10 bg-white/[0.02] p-8 text-center space-y-3">
              <PlugZap className="mx-auto h-8 w-8 text-white/30" />
              <p className="text-sm text-white/60">
                No MCP connections yet. Connect one from the marketplace or add a custom server.
              </p>
              <button
                type="button"
                onClick={() => setTab("all")}
                className="inline-flex items-center gap-2 rounded-xl bg-white/10 hover:bg-white/15 px-4 py-2 text-xs font-semibold text-white transition"
              >
                Browse Marketplace
              </button>
            </div>
          ) : (
            <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
              {installed.map((server) => (
                <InstalledCard
                  key={server.id}
                  server={server}
                  clientReady={clientReady}
                  onToggle={handleToggle}
                  onRefresh={async () => {
                    await refreshMCPServer(server.id);
                    await load();
                  }}
                  onDisconnect={async () => {
                    if (await averqelConfirm(`Disconnect MCP connection "${server.name}"?`)) {
                      await deleteMCPServer(server.id);
                      await load();
                    }
                  }}
                />
              ))}
            </div>
          )}
        </section>
      )}

      {/* VIEW: Marketplace Grid (All, Remote, Local) */}
      {tab !== "installed" && tab !== "custom" && (
        <section className="space-y-6">
          {loading ? (
            <div className="flex items-center justify-center py-16 text-sm text-slate-600 dark:text-white/60 gap-2">
              <Loader2 className="h-5 w-5 animate-spin text-emerald-500 dark:text-emerald-400" />
              Loading MCP marketplace connectors...
            </div>
          ) : displayItems.length === 0 ? (
            <div className="rounded-2xl border border-white/10 p-8 text-center space-y-3">
              <p className="text-sm text-white/55">No connectors match these filters.</p>
              <button
                type="button"
                onClick={clearFilters}
                className="inline-flex items-center gap-1.5 rounded-xl border border-white/10 px-3.5 py-1.5 text-xs text-white/70 hover:bg-white/10 transition"
              >
                Clear all filters
              </button>
            </div>
          ) : (
            <div className="grid items-stretch gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {displayItems.map((entry) => {
                const connectedServer = installed.find(
                  (server) =>
                    server.registry_entry_id === entry.id ||
                    (server.provider_slug && server.provider_slug === entry.provider_slug),
                );
                return (
                  <MCPMarketplaceCard
                    key={entry.id}
                    entry={entry}
                    connectedServer={connectedServer}
                    isConnecting={connectingEntryId === entry.id}
                    onConnect={(value) => void connect(value)}
                    onView={(value) => setSelectedDetailEntry(value)}
                    onToggle={handleToggle}
                    onManage={(server) => {
                      const entryForServer = items.find(
                        (i) =>
                          i.id === server.registry_entry_id ||
                          (server.provider_slug && i.provider_slug === server.provider_slug),
                      );
                      if (entryForServer) {
                        setSelectedDetailEntry(entryForServer);
                      } else {
                        router.push(`/dashboard/mcp/inspector/${encodeURIComponent(server.id)}`);
                      }
                    }}
                    onReconnect={(server) => {
                      void reconnect(server);
                    }}
                  />
                );
              })}
            </div>
          )}

          <Pagination
            page={page}
            hasNext={page < pages}
            onPrevious={() => setPage(Math.max(1, page - 1))}
            onNext={() => setPage(page + 1)}
          />
        </section>
      )}

      {showAddModal && (
        <AddCustomServerModal
          initialPreset={modalInitialPreset}
          onClose={() => {
            setShowAddModal(false);
            setModalInitialPreset(null);
          }}
          onCreated={async () => {
            setTab("custom");
            await load();
          }}
        />
      )}

      {selectedDetailEntry && (
        <MCPDetailDialog
          entry={selectedDetailEntry}
          connectedServer={installed.find(
            (server) =>
              server.registry_entry_id === selectedDetailEntry.id ||
              (server.provider_slug && server.provider_slug === selectedDetailEntry.provider_slug),
          )}
          connecting={connectingEntryId === selectedDetailEntry.id}
          onClose={() => setSelectedDetailEntry(null)}
          onConnect={async (entry) => {
            setConnectingEntryId(entry.id);
            try {
              await connect(entry);
            } finally {
              setConnectingEntryId(null);
            }
          }}
          onReconnect={async (server) => {
            await reconnect(server);
          }}
        />
      )}

      <ComposioConnectWarningModal
        isOpen={Boolean(composioWarning)}
        onClose={() => setComposioWarning(null)}
        entry={composioWarning?.entry ?? null}
        server={composioWarning?.server}
        message={composioWarning?.message}
      />
    </main>
  );
}

function AddCustomServerModal({
  initialPreset,
  onClose,
  onCreated,
}: {
  initialPreset?: PresetTemplate | null;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState(initialPreset?.name || "");
  const [transport, setTransport] = useState<"streamable_http" | "sse" | "stdio">(
    initialPreset?.transport || "stdio",
  );
  const [serverUrl, setServerUrl] = useState(initialPreset?.serverUrl || "");
  const [authToken, setAuthToken] = useState("");
  const [authHeader, setAuthHeader] = useState("Authorization");
  const [command, setCommand] = useState(initialPreset?.command || "npx");
  const [args, setArgs] = useState(initialPreset?.args || "");
  const [description, setDescription] = useState(initialPreset?.description || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applyPreset = (preset: PresetTemplate) => {
    setName(preset.name);
    setTransport(preset.transport);
    setCommand(preset.command || "npx");
    setArgs(preset.args || "");
    setServerUrl(preset.serverUrl || "");
    setDescription(preset.description);
    setError(null);
  };

  useEffect(() => {
    if (initialPreset) {
      applyPreset(initialPreset);
    }
  }, [initialPreset]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setError("Please provide a name for this MCP server.");
      return;
    }
    if (transport !== "stdio" && !serverUrl.trim()) {
      setError("Please provide a server endpoint URL starting with http:// or https://");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const parsedArgs = args
        .split(" ")
        .map((a) => a.trim())
        .filter(Boolean);
      await createCustomMCPServer({
        name: name.trim(),
        server_url: transport === "stdio" ? "stdio://local" : serverUrl.trim(),
        transport,
        auth_token: authToken.trim() || undefined,
        auth_header: authHeader.trim() || "Authorization",
        command: transport === "stdio" ? command.trim() : undefined,
        args: transport === "stdio" ? parsedArgs : undefined,
        description: description.trim() || undefined,
      });
      onCreated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to connect custom MCP server");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-md p-4 animate-in fade-in duration-150">
      <div className="w-full max-w-xl rounded-2xl border border-glass-border bg-surface-0/98 p-6 shadow-2xl space-y-5 animate-in zoom-in-95 duration-150 backdrop-blur-xl dark:border-white/15 dark:bg-[#0b1016]/98 text-slate-800 dark:text-white">
        <div className="flex items-start justify-between">
          <div className="space-y-1">
            <h2 className="text-xl font-bold text-slate-900 dark:text-white flex items-center gap-2">
              <Sparkles className="h-5 w-5 text-emerald-500 dark:text-emerald-400" /> Add Custom MCP Server
            </h2>
            <p className="text-xs text-slate-500 dark:text-white/60">
              Run local stdio CLI commands or connect custom SSE/HTTP endpoints directly.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-400 hover:text-slate-700 hover:bg-slate-100 dark:text-white/50 dark:hover:bg-white/10 dark:hover:text-white transition"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* 1-Click Preset Selector */}
        <div className="space-y-1.5">
          <label className="block text-[11px] font-semibold text-slate-600 dark:text-white/60 uppercase tracking-wider">
            1-Click Preset Templates
          </label>
          <div className="flex flex-wrap gap-1.5">
            {PRESET_TEMPLATES.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => applyPreset(p)}
                className="flex items-center gap-1.5 rounded-lg border border-glass-border bg-surface-1/80 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-emerald-50 hover:border-emerald-500/40 hover:text-emerald-950 dark:border-white/10 dark:bg-white/5 dark:text-white/80 dark:hover:bg-white/10 dark:hover:border-emerald-400/40 dark:hover:text-white transition"
              >
                <span>{p.icon}</span>
                <span>{p.name}</span>
              </button>
            ))}
          </div>
        </div>

        {error && (
          <div className="flex items-center gap-2 rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-700 dark:text-red-300">
            <AlertCircle className="h-4 w-4 shrink-0 text-red-500 dark:text-red-400" />
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-semibold text-slate-700 dark:text-white/80 mb-1.5">Server Name</label>
            <input
              type="text"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Postgres Local, Internal SSE API"
              className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 focus:border-emerald-500 focus:bg-surface-0 focus:outline-none focus:ring-1 focus:ring-emerald-500 dark:border-white/15 dark:bg-white/5 dark:text-white dark:placeholder-white/30 dark:focus:border-emerald-400 dark:focus:bg-white/[0.08] dark:focus:ring-emerald-400 transition"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 dark:text-white/80 mb-1.5">
              Transport Type
            </label>
            <div className="grid grid-cols-3 gap-2">
              {[
                { id: "stdio", label: "Local Stdio", desc: "CLI command on PC" },
                { id: "sse", label: "Remote SSE", desc: "Server-Sent Events" },
                { id: "streamable_http", label: "Streamable HTTP", desc: "HTTP POST endpoint" },
              ].map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setTransport(t.id as any)}
                  className={`rounded-xl border p-2.5 text-left transition ${
                    transport === t.id
                      ? "border-emerald-500/50 bg-emerald-500/12 text-emerald-950 dark:text-white dark:bg-emerald-500/20 dark:border-emerald-400/50 shadow-[0_0_12px_rgba(16,185,129,0.18)]"
                      : "border-glass-border bg-surface-1/70 text-slate-600 hover:bg-surface-2 hover:text-slate-900 dark:border-white/10 dark:bg-white/5 dark:text-white/70 dark:hover:bg-white/10 dark:hover:text-white"
                  }`}
                >
                  <div className="text-xs font-semibold">{t.label}</div>
                  <div className={`text-[10px] ${transport === t.id ? "text-emerald-700 dark:text-emerald-300/80" : "text-slate-400 dark:text-white/45"}`}>{t.desc}</div>
                </button>
              ))}
            </div>
          </div>

          {transport !== "stdio" ? (
            <div>
              <label className="block text-xs font-semibold text-slate-700 dark:text-white/80 mb-1.5">
                Endpoint URL
              </label>
              <input
                type="url"
                required
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                placeholder="http://localhost:8000/sse or https://mcp.domain.com"
                className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 font-mono text-xs focus:border-emerald-500 focus:bg-surface-0 focus:outline-none focus:ring-1 focus:ring-emerald-500 dark:border-white/15 dark:bg-white/5 dark:text-white dark:placeholder-white/30 dark:focus:border-emerald-400 dark:focus:bg-white/[0.08] dark:focus:ring-emerald-400 transition"
              />
            </div>
          ) : (
            <div className="grid grid-cols-3 gap-2">
              <div>
                <label className="block text-xs font-semibold text-slate-700 dark:text-white/80 mb-1.5">Command</label>
                <input
                  type="text"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="npx"
                  className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-3 py-2 text-xs text-slate-900 placeholder-slate-400 font-mono focus:border-emerald-500 focus:bg-surface-0 focus:outline-none dark:border-white/15 dark:bg-white/5 dark:text-white dark:placeholder-white/30 dark:focus:border-emerald-400"
                />
              </div>
              <div className="col-span-2">
                <label className="block text-xs font-semibold text-slate-700 dark:text-white/80 mb-1.5">
                  Arguments
                </label>
                <input
                  type="text"
                  value={args}
                  onChange={(e) => setArgs(e.target.value)}
                  placeholder="-y @modelcontextprotocol/server-postgres"
                  className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-3 py-2 text-xs text-slate-900 placeholder-slate-400 font-mono focus:border-emerald-500 focus:bg-surface-0 focus:outline-none dark:border-white/15 dark:bg-white/5 dark:text-white dark:placeholder-white/30 dark:focus:border-emerald-400"
                />
              </div>
            </div>
          )}

          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="block text-xs font-semibold text-slate-700 dark:text-white/80">
                Authorization Token / API Key{" "}
                <span className="text-slate-400 dark:text-white/40 font-normal">(Optional)</span>
              </label>
              <input
                type="text"
                value={authHeader}
                onChange={(e) => setAuthHeader(e.target.value)}
                title="Header name"
                placeholder="Header (e.g. Authorization)"
                className="w-32 rounded border border-glass-border bg-surface-1/90 px-2 py-0.5 text-[10px] text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-white/70 font-mono text-right focus:border-emerald-500 dark:focus:border-emerald-400 focus:outline-none"
              />
            </div>
            <div className="relative">
              <Key className="absolute left-3 top-3 h-4 w-4 text-slate-400 dark:text-white/40" />
              <input
                type="password"
                value={authToken}
                onChange={(e) => setAuthToken(e.target.value)}
                placeholder="secret_..., bearer token, or API key"
                className="w-full rounded-xl border border-glass-border bg-surface-1/90 pl-9 pr-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 font-mono text-xs focus:border-emerald-500 focus:bg-surface-0 focus:outline-none focus:ring-1 focus:ring-emerald-500 dark:border-white/15 dark:bg-white/5 dark:text-white dark:placeholder-white/30 dark:focus:border-emerald-400 dark:focus:bg-white/[0.08] dark:focus:ring-emerald-400 transition"
              />
            </div>
            <p className="mt-1 text-[11px] text-slate-500 dark:text-white/45">
              Stored securely in local SQLite database with AES-256 encryption.
            </p>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 dark:text-white/80 mb-1.5">
              Description <span className="text-slate-400 dark:text-white/40 font-normal">(Optional)</span>
            </label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="e.g. Postgres DB queries, customer records..."
              className="w-full rounded-xl border border-glass-border bg-surface-1/90 px-3.5 py-2 text-xs text-slate-900 placeholder-slate-400 focus:border-emerald-500 focus:bg-surface-0 focus:outline-none dark:border-white/15 dark:bg-white/5 dark:text-white dark:placeholder-white/30 dark:focus:border-emerald-400"
            />
          </div>

          <div className="flex items-center justify-end gap-2.5 pt-3 border-t border-glass-border dark:border-white/10">
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="rounded-xl border border-glass-border bg-surface-1 px-4 py-2 text-xs font-medium text-slate-700 hover:bg-surface-2 hover:text-slate-900 dark:border-white/15 dark:bg-white/5 dark:text-white/70 dark:hover:bg-white/10 dark:hover:text-white transition"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="flex items-center gap-2 rounded-xl bg-emerald-500 hover:bg-emerald-400 px-5 py-2 text-xs font-semibold text-slate-950 transition shadow-[0_0_15px_rgba(16,185,129,0.3)] disabled:opacity-50 active:scale-95"
            >
              {saving ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Connecting & Discovering...
                </>
              ) : (
                "Save & Connect Server"
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Chip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`rounded-full border px-3.5 py-1.5 text-xs transition-all active:scale-95 ${
        active
          ? "border-emerald-600 bg-emerald-600 text-white font-bold shadow-xs hover:bg-emerald-700 dark:border-emerald-400 dark:bg-emerald-500 dark:text-slate-950 dark:font-extrabold dark:shadow-[0_0_12px_rgba(16,185,129,0.3)]"
          : "border-slate-300 bg-white text-slate-800 hover:text-slate-950 hover:bg-slate-100 shadow-2xs dark:border-white/15 dark:bg-slate-900/60 dark:text-slate-200 dark:hover:bg-white/10 font-bold"
      }`}
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function CustomServerCard({
  server,
  clientReady,
  onToggle,
  onRefresh,
  onDisconnect,
}: {
  server: MCPConnection;
  clientReady: boolean;
  onToggle: (server: MCPConnection) => Promise<void>;
  onRefresh: () => Promise<void>;
  onDisconnect: () => Promise<void>;
}) {
  const [copied, setCopied] = useState(false);
  const config = server.config || {};
  const isStdio = server.transport === "stdio";
  const cmdLine = isStdio
    ? `${config.command || "npx"} ${(config.args || []).join(" ")}`.trim()
    : config.server_url || "";

  const copyCmd = () => {
    if (!cmdLine) return;
    void navigator.clipboard.writeText(cmdLine);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const statusTone =
    server.status === "connected"
      ? "text-emerald-900 bg-emerald-100 border-emerald-400 font-bold dark:text-emerald-300 dark:bg-emerald-500/20 dark:border-emerald-400/40"
      : server.status === "failed"
        ? "text-red-900 bg-red-100 border-red-400 font-bold dark:text-red-300 dark:bg-red-500/20 dark:border-red-400/40"
        : "text-amber-950 bg-amber-100 border-amber-400 font-bold dark:text-amber-300 dark:bg-amber-500/20 dark:border-amber-400/40";

  return (
    <article className="mcp-installed-card rounded-2xl border border-slate-200/90 bg-white p-5 space-y-4 shadow-sm hover:border-emerald-500/50 dark:border-white/10 dark:bg-slate-900/60 dark:shadow-lg dark:hover:border-emerald-400/30 transition">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <h3 className="text-base font-bold text-slate-950 dark:text-white">{server.name}</h3>
            <span className="rounded-full border border-emerald-400 bg-emerald-50 px-2.5 py-0.5 text-[10px] font-bold text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/15 dark:text-emerald-300">
              Custom {transportLabel(server.transport)}
            </span>
          </div>
          <p className="text-xs text-slate-600 dark:text-white/50">
            {Number(config.mcp_catalog_tool_count || 0)} tools registered
            {clientReady && config.mcp_catalog_last_sync_at
              ? ` · Refreshed ${formatRelative(config.mcp_catalog_last_sync_at)}`
              : ""}
          </p>
        </div>

        {/* Toggle Switch */}
        <div className="flex items-center gap-2.5">
          <MCPToggleSwitch
            checked={Boolean(server.enabled)}
            onChange={() => void onToggle(server)}
            label={server.enabled ? "Active" : "Disabled"}
            title={server.enabled ? "Active in DeepSpace. Click to disable." : "Paused. Click to enable."}
          />
          <span
            className={`rounded-full border px-2.5 py-0.5 text-[10px] font-semibold uppercase ${statusTone}`}
          >
            {server.status}
          </span>
        </div>
      </div>

      {/* Command / Endpoint display box */}
      {cmdLine && (
        <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-slate-100 px-3 py-2 text-xs font-mono text-slate-800 dark:border-white/10 dark:bg-black/40 dark:text-white/80">
          <span className="truncate mr-2 text-[11px] text-emerald-800 dark:text-emerald-300 font-bold">{cmdLine}</span>
          <button
            type="button"
            onClick={copyCmd}
            title="Copy command"
            className="shrink-0 p-1 rounded hover:bg-slate-200 dark:hover:bg-white/10 text-slate-600 dark:text-white/50 hover:text-slate-950 dark:hover:text-white transition"
          >
            {copied ? <Check className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
          </button>
        </div>
      )}

      <div className="pt-2 border-t border-slate-200 dark:border-white/10 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <a
            href={`/dashboard/mcp/inspector/${encodeURIComponent(server.id)}`}
            className="rounded-xl border border-slate-300 hover:border-slate-400 bg-white hover:bg-slate-100 px-3 py-1.5 text-xs font-bold text-slate-800 hover:text-slate-950 shadow-2xs dark:border-white/15 dark:bg-white/5 dark:text-white/80 dark:hover:bg-white/10 transition"
          >
            Manage Tools
          </a>
          <button
            type="button"
            className="flex items-center gap-1 rounded-xl border border-slate-300 hover:border-slate-400 bg-white hover:bg-slate-100 px-3 py-1.5 text-xs font-bold text-slate-800 hover:text-slate-950 shadow-2xs dark:border-white/15 dark:bg-white/5 dark:text-white/80 dark:hover:bg-white/10 transition"
            onClick={() => void onRefresh()}
          >
            <RefreshCw className="h-3 w-3" /> Refresh
          </button>
        </div>
        <button
          type="button"
          className="flex items-center gap-1 rounded-xl border border-red-300 hover:border-red-400 bg-red-50 hover:bg-red-100 px-3 py-1.5 text-xs font-bold text-red-700 shadow-2xs dark:border-red-500/40 dark:bg-red-500/10 dark:text-red-300 dark:hover:bg-red-500/20 transition"
          onClick={() => void onDisconnect()}
        >
          <Trash2 className="h-3 w-3" /> Disconnect
        </button>
      </div>
    </article>
  );
}

function InstalledCard({
  server,
  clientReady,
  onToggle,
  onRefresh,
  onDisconnect,
}: {
  server: MCPConnection;
  clientReady: boolean;
  onToggle: (server: MCPConnection) => Promise<void>;
  onRefresh: () => Promise<void>;
  onDisconnect: () => Promise<void>;
}) {
  const config = server.config || {};
  const statusTone =
    server.status === "connected"
      ? "text-emerald-900 bg-emerald-100 border-emerald-400 font-bold dark:text-emerald-300 dark:bg-emerald-500/20 dark:border-emerald-400/40"
      : server.status === "failed"
        ? "text-red-900 bg-red-100 border-red-400 font-bold dark:text-red-300 dark:bg-red-500/20 dark:border-red-400/40"
        : "text-amber-950 bg-amber-100 border-amber-400 font-bold dark:text-amber-300 dark:bg-amber-500/20 dark:border-amber-400/40";

  return (
    <article className="mcp-installed-card rounded-2xl border border-slate-200/90 bg-white p-5 space-y-4 shadow-sm hover:border-emerald-500/50 dark:border-white/10 dark:bg-white/[0.04] transition">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-bold text-slate-950 dark:text-white">{server.name}</h2>
          <p className="mt-1 text-sm text-slate-700 dark:text-white/60">
            {transportLabel(server.transport)} ·{" "}
            {server.config?.oauth_mode === "none" ? "Anonymous" : "OAuth"} ·{" "}
            {Number(config.mcp_catalog_tool_count || 0)} tools
          </p>
          <p className="mt-1 text-xs text-slate-500 dark:text-white/45">
            Catalog revision {server.catalog_revision || 0}
            {clientReady && config.mcp_catalog_last_sync_at
              ? ` · Last refreshed ${formatRelative(config.mcp_catalog_last_sync_at)}`
              : ""}
          </p>
          {server.account_identity?.email && (
            <p className="mt-2 text-sm font-semibold text-slate-900 dark:text-white/75">
              Connected account: {server.account_identity.email}
            </p>
          )}
        </div>

        {/* Status + Toggle Switch */}
        <div className="flex flex-col items-end gap-2">
          <MCPToggleSwitch
            checked={Boolean(server.enabled)}
            onChange={() => void onToggle(server)}
            label={server.enabled ? "Active" : "Disabled"}
            title={server.enabled ? "Active in DeepSpace. Click to disable." : "Paused. Click to enable."}
          />
          <span
            className={`rounded-full border px-2.5 py-0.5 text-[10px] font-semibold uppercase ${statusTone}`}
          >
            {server.status}
          </span>
        </div>
      </div>

      <div>
        <MCPHealthStatus
          health={{
            status: server.status === "connected" ? "healthy" : server.status,
            last_checked_at: config.mcp_catalog_last_sync_at,
          }}
          compact
        />
      </div>

      <div className="flex flex-wrap gap-2 pt-2 border-t border-slate-200 dark:border-white/10">
        <a
          href={`/dashboard/mcp/inspector/${encodeURIComponent(server.id)}`}
          className="rounded-xl border border-slate-300 hover:border-slate-400 bg-white hover:bg-slate-100 px-4 py-2 text-sm font-bold text-slate-800 hover:text-slate-950 shadow-2xs dark:border-white/15 dark:bg-white/5 dark:text-white/80 dark:hover:bg-white/10 transition"
        >
          Manage connection
        </a>
        <button
          type="button"
          className="rounded-xl border border-slate-300 hover:border-slate-400 bg-white hover:bg-slate-100 px-4 py-2 text-sm font-bold text-slate-800 hover:text-slate-950 shadow-2xs dark:border-white/15 dark:bg-white/5 dark:text-white/80 dark:hover:bg-white/10 transition"
          onClick={() => void onRefresh()}
        >
          Refresh catalog
        </button>
        <button
          type="button"
          className="rounded-xl border border-red-300 hover:border-red-400 bg-red-50 hover:bg-red-100 px-4 py-2 text-sm font-bold text-red-700 shadow-2xs dark:border-red-500/40 dark:bg-red-500/10 dark:text-red-300 dark:hover:bg-red-500/20 transition"
          onClick={() => void onDisconnect()}
        >
          Disconnect
        </button>
      </div>
    </article>
  );
}

function Pagination({
  page,
  hasNext,
  onPrevious,
  onNext,
}: {
  page: number;
  hasNext: boolean;
  onPrevious: () => void;
  onNext: () => void;
}) {
  return (
    <div className="flex items-center justify-end gap-2">
      <button
        type="button"
        disabled={page <= 1}
        onClick={onPrevious}
        className="rounded-xl border border-white/10 px-3 py-2 text-xs text-white/70 hover:bg-white/5 disabled:opacity-30 transition"
      >
        Previous
      </button>
      <span className="text-xs text-white/45">Page {page}</span>
      <button
        type="button"
        disabled={!hasNext}
        onClick={onNext}
        className="rounded-xl border border-white/10 px-3 py-2 text-xs text-white/70 hover:bg-white/5 disabled:opacity-30 transition"
      >
        Next
      </button>
    </div>
  );
}

function transportLabel(value?: string | null): string {
  switch (value) {
    case "streamable_http":
      return "Remote HTTP";
    case "sse":
      return "Remote SSE";
    case "stdio":
      return "Local Stdio";
    case "ssh":
      return "Remote SSH";
    default:
      return value ? value.replaceAll("_", " ") : "Remote";
  }
}

function formatRelative(value?: string | null): string {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  const days = Math.round((date.getTime() - Date.now()) / 86400000);
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(days, "day");
}

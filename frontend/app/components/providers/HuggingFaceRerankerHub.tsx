"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Download,
  ExternalLink,
  Search,
  CheckCircle2,
  Loader2,
  HardDrive,
  Layers,
  Sparkles,
  Trash2,
  ArrowRight,
  Globe2,
  FileCode,
  Zap,
} from "lucide-react";
import toast from "react-hot-toast";

import {
  fetchRerankerHub,
  installRerankerHubModel,
  uninstallRerankerHubModel,
  type OpenSourceRerankerModel,
} from "@/lib/providers-api";

interface HuggingFaceRerankerHubProps {
  activeModelName?: string | null;
  onSelectModel: (modelName: string) => void;
  className?: string;
}

export default function HuggingFaceRerankerHub({
  activeModelName,
  onSelectModel,
  className = "",
}: HuggingFaceRerankerHubProps) {
  const [loading, setLoading] = useState(true);
  const [models, setModels] = useState<OpenSourceRerankerModel[]>([]);
  const [storageDir, setStorageDir] = useState("~/.averqel-harness/models/rerankers");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedCategory, setSelectedCategory] = useState<string>("all");
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [uninstallingId, setUninstallingId] = useState<string | null>(null);
  const [customModelInput, setCustomModelInput] = useState("");
  const [customDownloading, setCustomDownloading] = useState(false);

  const loadHub = async () => {
    try {
      setLoading(true);
      const data = await fetchRerankerHub();
      setModels(data.models);
      if (data.storage_directory) {
        setStorageDir(data.storage_directory);
      }
    } catch {
      toast.error("Failed to load Hugging Face reranker models hub");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadHub();
  }, []);

  const handleInstall = async (modelId: string) => {
    setDownloadingId(modelId);
    try {
      const res = await installRerankerHubModel(modelId);
      toast.success(res.message || `Installed ${modelId} into ~/.averqel-harness/models/rerankers/`);
      setModels((prev) =>
        prev.map((m) =>
          m.model_id === modelId ? { ...m, is_installed: true, local_path: res.model.local_path } : m,
        ),
      );
      onSelectModel(modelId);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloadingId(null);
    }
  };

  const handleUninstall = async (modelId: string) => {
    setUninstallingId(modelId);
    try {
      await uninstallRerankerHubModel(modelId);
      toast.success(`Removed ${modelId} from local storage`);
      setModels((prev) =>
        prev.map((m) => (m.model_id === modelId ? { ...m, is_installed: false, local_path: null } : m)),
      );
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to uninstall reranker model");
    } finally {
      setUninstallingId(null);
    }
  };

  const handleCustomInstall = async (e: React.FormEvent) => {
    e.preventDefault();
    const id = customModelInput.trim();
    if (!id) {
      toast.error("Please enter a valid Hugging Face reranker model repository ID");
      return;
    }
    setCustomDownloading(true);
    try {
      await installRerankerHubModel(id);
      toast.success(`Downloaded custom reranker ${id} into ~/.averqel-harness/models/rerankers/!`);
      setCustomModelInput("");
      await loadHub();
      onSelectModel(id);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to download custom reranker model");
    } finally {
      setCustomDownloading(false);
    }
  };

  const handleOpenExternal = (url: string, e?: React.MouseEvent) => {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    try {
      const electronBridge = (window as unknown as { electron?: { openExternal?: (u: string) => Promise<boolean> } })?.electron;
      if (electronBridge?.openExternal) {
        void electronBridge.openExternal(url);
        return;
      }
    } catch {
      // ignore
    }
    if (typeof window !== "undefined") {
      window.open(url, "_blank", "noopener,noreferrer");
    }
  };

  const filteredModels = useMemo(() => {
    return models.filter((m) => {
      const query = searchQuery.toLowerCase().trim();
      const matchesQuery =
        !query ||
        m.model_id.toLowerCase().includes(query) ||
        m.display_name.toLowerCase().includes(query) ||
        m.author.toLowerCase().includes(query) ||
        m.description.toLowerCase().includes(query);

      if (!matchesQuery) return false;

      if (selectedCategory === "all") return true;
      if (selectedCategory === "installed") return m.is_installed;
      if (selectedCategory === "lightweight") return m.size_mb <= 150;
      if (selectedCategory === "sota") return m.category === "sota";
      if (selectedCategory === "multilingual") return m.category === "multilingual";
      return true;
    });
  }, [models, searchQuery, selectedCategory]);

  return (
    <div
      className={`hf-hub-shell relative overflow-hidden rounded-[1.5rem] border border-glass-border bg-surface-0/95 p-6 shadow-sm space-y-6 ${className}`}
    >
      {/* Header Banner */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-start gap-3.5">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-emerald-600 to-teal-600 text-white shadow-xs dark:from-emerald-500 dark:to-teal-500 dark:text-slate-950 text-xl">
            🎯
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-slate-900 dark:text-white text-base sm:text-lg font-black tracking-tight">
                Hugging Face Open-Source Reranker Hub
              </h3>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-teal-400/50 bg-teal-50 px-3 py-1 text-[10px] font-black tracking-wide text-teal-900 dark:border-teal-500/40 dark:bg-teal-950/60 dark:text-teal-300 shadow-2xs">
                <Sparkles className="h-3 w-3 text-teal-600 dark:text-teal-400" />
                Cross-Encoder & Local
              </span>
            </div>
            <p className="text-slate-600 dark:text-slate-300 text-xs leading-relaxed mt-1 max-w-2xl font-medium">
              Discover and download neural cross-encoder rerankers directly into{" "}
              <code className="text-slate-900 dark:text-white font-mono text-[11px] bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 rounded-md border border-slate-200 dark:border-white/10 font-bold">
                ~/.averqel-harness/models/rerankers/
              </code>{" "}
              for accurate query-document relevance scoring with zero API latency or cost.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <a
            href="https://huggingface.co/models?pipeline_tag=text-classification&search=reranker"
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => handleOpenExternal("https://huggingface.co/models?pipeline_tag=text-classification&search=reranker", e)}
            className="inline-flex items-center gap-1.5 rounded-xl border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-800 px-3.5 py-1.5 text-xs font-bold text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-700 transition-all shadow-2xs cursor-pointer"
          >
            <span>Explore on HF</span>
            <ExternalLink className="h-3.5 w-3.5 text-slate-500" />
          </a>
        </div>
      </div>

      {/* Search & Category Filter Controls */}
      <div className="space-y-2.5 pt-1">
        <div className="relative flex items-center rounded-xl border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-900 px-3.5 py-2 shadow-2xs transition-all focus-within:border-emerald-500 focus-within:ring-2 focus-within:ring-emerald-500/20">
          <Search className="mr-2.5 h-4 w-4 shrink-0 text-slate-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search rerankers by name, author (e.g. bge, ms-marco, jina, mxbai, tinybert)..."
            className="search-box-input w-full min-w-0 bg-transparent text-xs text-slate-900 dark:text-white placeholder:text-slate-400 font-medium border-0 outline-none ring-0 focus:outline-none focus:ring-0 !border-0 !border-none !bg-transparent p-0"
          />
          {searchQuery ? (
            <button
              type="button"
              onClick={() => setSearchQuery("")}
              className="ml-2 text-xs text-slate-400 hover:text-slate-700 dark:hover:text-white transition-colors shrink-0"
              title="Clear search"
            >
              ✕
            </button>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-1.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-100/90 dark:bg-slate-900/80 p-1.5 shadow-inner">
          {[
            { id: "all", label: `All Rerankers (${models.length})` },
            { id: "installed", label: `✓ Installed (${models.filter((m) => m.is_installed).length})` },
            { id: "lightweight", label: "⚡ Lightweight (<150MB)" },
            { id: "sota", label: "🎯 SOTA Accuracy" },
            { id: "multilingual", label: "🌐 Multilingual" },
          ].map((cat) => (
            <button
              key={cat.id}
              type="button"
              onClick={() => setSelectedCategory(cat.id)}
              className={`rounded-lg px-3.5 py-1.5 text-xs transition-all font-bold ${
                selectedCategory === cat.id
                  ? "bg-gradient-to-r from-emerald-600 to-teal-600 !text-white shadow-xs dark:from-emerald-500 dark:to-teal-500 dark:!text-slate-950 font-black"
                  : "text-slate-700 dark:text-slate-300 hover:text-slate-950 dark:hover:text-white hover:bg-white dark:hover:bg-slate-800"
              }`}
            >
              {cat.label}
            </button>
          ))}
        </div>
      </div>

      {/* Model Grid */}
      {loading ? (
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground space-y-2">
          <Loader2 className="h-6 w-6 animate-spin text-primary" />
          <p className="text-xs font-mono">Scanning ~/.averqel-harness/models/rerankers & Hugging Face...</p>
        </div>
      ) : filteredModels.length === 0 ? (
        <div className="rounded-2xl border border-glass-border bg-surface-1/40 p-8 text-center text-xs text-muted-foreground space-y-1 font-mono">
          <p className="font-bold text-foreground">No reranker models match the current filter</p>
          <p>Try clearing your search query or download a custom Hugging Face reranker repository below.</p>
        </div>
      ) : (
        <div className="grid gap-3.5 sm:grid-cols-2">
          {filteredModels.map((m) => {
            const isInstalled = m.is_installed;
            const isSelected = activeModelName === m.model_id;
            const isDownloading = downloadingId === m.model_id;
            const isUninstalling = uninstallingId === m.model_id;

            return (
              <div
                key={m.model_id}
                className={`hf-model-card group relative flex flex-col justify-between rounded-2xl border p-5 transition-all duration-200 ${
                  isSelected
                    ? "border-emerald-500 dark:border-emerald-400 bg-white dark:bg-slate-900 shadow-sm ring-2 ring-emerald-500/30"
                    : "border-slate-200 dark:border-white/10 bg-white dark:bg-slate-900/80 hover:border-slate-300 dark:hover:border-white/20 hover:shadow-xs"
                }`}
              >
                <div>
                  {/* Top Bar: Author, HF Link & Installed Status */}
                  <div className="flex items-start justify-between gap-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] font-black text-slate-500 dark:text-slate-400 tracking-wider uppercase">
                          {m.author}
                        </span>
                        <a
                          href={m.huggingface_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => handleOpenExternal(m.huggingface_url, e)}
                          className="inline-flex items-center gap-1 rounded-full border border-amber-400/60 bg-amber-50 dark:border-amber-500/30 dark:bg-amber-950/40 px-2 py-0.5 text-[10px] font-black text-amber-800 dark:text-amber-300 hover:bg-amber-100 dark:hover:bg-amber-900/50 transition-colors shadow-2xs"
                          title={`Open ${m.model_id} on Hugging Face`}
                        >
                          <span>HF</span>
                          <ExternalLink className="h-2.5 w-2.5 opacity-80" />
                        </a>
                      </div>
                      <h4
                        className="mt-1 text-sm font-black tracking-tight text-slate-900 dark:text-white truncate group-hover:text-emerald-700 dark:group-hover:text-emerald-400 transition-colors"
                        title={m.model_id}
                      >
                        {m.display_name}
                      </h4>
                    </div>

                    {isInstalled ? (
                      <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-400/60 bg-emerald-50 dark:border-emerald-500/40 dark:bg-emerald-950/60 px-2.5 py-0.5 text-[10px] font-black text-emerald-800 dark:text-emerald-300 shrink-0 shadow-2xs">
                        <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
                        Local
                      </span>
                    ) : (
                      <span className="inline-flex items-center rounded-full border border-slate-200 dark:border-white/10 bg-slate-100 dark:bg-slate-800 px-2.5 py-0.5 text-[10px] font-black text-slate-700 dark:text-slate-300 tabular-nums shrink-0">
                        {m.size_mb} MB
                      </span>
                    )}
                  </div>

                  {/* Description */}
                  <p className="mt-2 text-xs text-slate-600 dark:text-slate-300 font-medium leading-relaxed line-clamp-2 h-8">
                    {m.description}
                  </p>

                  {/* Specs Chips */}
                  <div className="mt-3 flex flex-wrap items-center gap-1.5">
                    <span className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-slate-800 px-2.5 py-1 text-[11px] font-black text-slate-800 dark:text-slate-200">
                      <Layers className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
                      <span>{m.architecture}</span>
                    </span>
                    <span className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-slate-800 px-2.5 py-1 text-[11px] font-black text-slate-800 dark:text-slate-200">
                      <FileCode className="h-3 w-3 text-cyan-600 dark:text-cyan-400" />
                      <span>{m.context_window} ctx</span>
                    </span>
                    <span className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-slate-800 px-2.5 py-1 text-[11px] font-black text-slate-800 dark:text-slate-200">
                      <Zap className="h-3 w-3 text-amber-600 dark:text-amber-400" />
                      <span className="capitalize">{m.speed_tier.replace("_", " ")}</span>
                    </span>
                  </div>
                </div>

                {/* Actions Footer */}
                <div className="mt-4 pt-3.5 border-t border-slate-200 dark:border-white/10 flex items-center gap-2">
                  {isInstalled ? (
                    <>
                      <button
                        type="button"
                        onClick={() => onSelectModel(m.model_id)}
                        className={`inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-xs font-black transition-all flex-1 ${
                          isSelected
                            ? "bg-gradient-to-r from-emerald-600 to-teal-600 !text-white shadow-xs dark:from-emerald-500 dark:to-teal-500 dark:!text-slate-950"
                            : "border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-700 shadow-2xs"
                        }`}
                      >
                        {isSelected ? (
                          <>
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            <span>Active Reranker</span>
                          </>
                        ) : (
                          <>
                            <span>Select Reranker</span>
                            <ArrowRight className="h-3.5 w-3.5" />
                          </>
                        )}
                      </button>

                      <button
                        type="button"
                        onClick={(e) => handleOpenExternal(m.huggingface_url, e)}
                        className="inline-flex h-9 w-9 items-center justify-center rounded-xl border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:text-amber-600 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors shrink-0 shadow-2xs"
                        title={`Open ${m.model_id} on Hugging Face`}
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                      </button>

                      <button
                        type="button"
                        disabled={isUninstalling}
                        onClick={() => handleUninstall(m.model_id)}
                        className="inline-flex h-9 w-9 items-center justify-center rounded-xl border border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-950/40 text-rose-700 dark:text-rose-300 hover:bg-rose-100 dark:hover:bg-rose-900/50 transition-colors shrink-0 disabled:opacity-50 shadow-2xs"
                        title="Delete from ~/.averqel-harness/models/rerankers"
                      >
                        {isUninstalling ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Trash2 className="h-3.5 w-3.5" />
                        )}
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        disabled={isDownloading}
                        onClick={() => handleInstall(m.model_id)}
                        className="inline-flex items-center justify-center gap-2 rounded-xl border border-emerald-500/40 bg-emerald-50 hover:bg-emerald-100 dark:border-emerald-500/30 dark:bg-emerald-950/40 dark:hover:bg-emerald-900/50 px-4 py-2.5 text-xs font-black text-emerald-900 dark:text-emerald-300 transition-all flex-1 disabled:opacity-50 shadow-2xs"
                      >
                        {isDownloading ? (
                          <>
                            <Loader2 className="h-3.5 w-3.5 animate-spin text-emerald-700 dark:text-emerald-400" />
                            <span>Downloading to ~/.averqel-harness…</span>
                          </>
                        ) : (
                          <>
                            <Download className="h-3.5 w-3.5 text-emerald-700 dark:text-emerald-400" />
                            <span>Download & Install ({m.size_mb} MB)</span>
                          </>
                        )}
                      </button>

                      <button
                        type="button"
                        onClick={(e) => handleOpenExternal(m.huggingface_url, e)}
                        className="inline-flex h-9 w-9 items-center justify-center rounded-xl border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:text-amber-600 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors shrink-0 shadow-2xs"
                        title={`Open ${m.model_id} on Hugging Face`}
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                      </button>
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Custom Hugging Face Reranker Model Downloader Section */}
      <div className="rounded-2xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-slate-900/60 p-5 space-y-3 shadow-2xs">
        <div>
          <h4 className="text-slate-900 dark:text-white text-xs font-black tracking-tight flex items-center gap-2">
            <HardDrive className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
            <span>Install Any Custom Hugging Face Reranker</span>
          </h4>
          <p className="text-slate-600 dark:text-slate-300 text-xs leading-relaxed mt-1">
            Need a domain-specific cross-encoder? Enter any Hugging Face model repository ID (e.g.{" "}
            <code className="text-slate-900 dark:text-white font-mono text-[11px] bg-white dark:bg-slate-800 px-1 py-0.5 rounded border border-slate-200 dark:border-white/10 font-bold">BAAI/bge-reranker-v2-m3</code> or{" "}
            <code className="text-slate-900 dark:text-white font-mono text-[11px] bg-white dark:bg-slate-800 px-1 py-0.5 rounded border border-slate-200 dark:border-white/10 font-bold">mixedbread-ai/mxbai-rerank-base-v1</code>).
          </p>
        </div>

        <form onSubmit={handleCustomInstall} className="flex flex-col sm:flex-row gap-2 pt-1">
          <input
            type="text"
            value={customModelInput}
            onChange={(e) => setCustomModelInput(e.target.value)}
            placeholder="e.g. BAAI/bge-reranker-v2-m3"
            className="flex-1 rounded-xl border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-900 px-3.5 py-2.5 text-xs font-mono font-bold text-slate-900 dark:text-white placeholder:text-slate-400 focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/20 outline-none transition-all shadow-2xs"
          />
          <button
            type="submit"
            disabled={customDownloading || !customModelInput.trim()}
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-700 hover:to-teal-700 !text-white dark:from-emerald-500 dark:to-teal-500 dark:!text-slate-950 px-5 py-2.5 text-xs font-black transition-all shadow-xs disabled:opacity-40 shrink-0"
          >
            {customDownloading ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>Downloading…</span>
              </>
            ) : (
              <>
                <Download className="h-3.5 w-3.5" />
                <span>Download to ~/.averqel-harness</span>
              </>
            )}
          </button>
        </form>
      </div>
    </div>
  );
}

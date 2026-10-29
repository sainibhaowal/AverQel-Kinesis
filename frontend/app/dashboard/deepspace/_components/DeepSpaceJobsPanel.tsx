"use client";

import { useEffect, useState, useMemo, useCallback } from "react";
import {
  Briefcase,
  FileCode,
  X,
  Loader2,
  AlertTriangle,
  CheckCircle,
  Clock,
  RefreshCw,
  Copy,
  Check,
  ChevronDown,
  ChevronRight,
  Filter,
} from "lucide-react";
import { fetchWithAuth } from "@/lib/api";

type JobKind = "turn" | "artifact";
type JobStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

interface Job {
  job_id: string;
  kind: JobKind;
  status: JobStatus;
  label: string;
  priority?: number;
  sequence?: number;
  format?: string;
  file_id?: string | null;
  error?: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

interface DeepSpaceJobsPanelProps {
  conversationId: string | null;
}

const STATUS_ICON: Record<JobStatus, typeof CheckCircle> = {
  queued: Clock,
  running: Loader2,
  completed: CheckCircle,
  failed: AlertTriangle,
  cancelled: X,
};

const STATUS_COLOR: Record<JobStatus, string> = {
  queued: "text-amber-400 border-amber-500/30 bg-amber-500/10",
  running: "text-cyan-400 border-cyan-500/30 bg-cyan-500/10",
  completed: "text-emerald-400 border-emerald-500/30 bg-emerald-500/10",
  failed: "text-rose-400 border-rose-500/30 bg-rose-500/10",
  cancelled: "text-muted-foreground border-border-subtle bg-surface-2",
};

const KIND_LABEL: Record<JobKind, string> = {
  turn: "Prompt Turn",
  artifact: "Media Artifact",
};

function formatDuration(start: string | null, end: string | null): string | null {
  if (!start) return null;
  const s = new Date(start).getTime();
  const e = end ? new Date(end).getTime() : Date.now();
  if (isNaN(s) || isNaN(e) || e < s) return null;
  const ms = e - s;
  if (ms < 1000) return `${ms}ms`;
  const sec = ms / 1000;
  return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)}s`;
}

export function DeepSpaceJobsPanel({ conversationId }: DeepSpaceJobsPanelProps) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<"all" | JobStatus>("all");
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!conversationId) return;
    const url = `/deepspace/chats/${encodeURIComponent(conversationId)}/jobs?limit=100`;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchWithAuth(url, { method: "GET" });
      if (!response.ok) throw new Error("Failed to load jobs");
      let data: unknown = [];
      if (typeof response.text === "function") {
        const body = await response.text();
        if (body.trim()) {
          try {
            data = JSON.parse(body) as unknown;
          } catch {
            throw new Error("Jobs returned an invalid response.");
          }
        }
      } else {
        data = await response.json();
      }
      setJobs(Array.isArray(data) ? data : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
      setJobs([]);
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    load();
  }, [load]);

  const copyToClipboard = async (text: string, id: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(id);
      setTimeout(() => setCopiedId(null), 2000);
    } catch {}
  };

  const filteredJobs = useMemo(() => {
    if (statusFilter === "all") return jobs;
    return jobs.filter((j) => j.status === statusFilter);
  }, [jobs, statusFilter]);

  const activeCount = useMemo(() => {
    return jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  }, [jobs]);

  if (!conversationId) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-[11px] text-muted-foreground">
        Open a conversation to inspect its background jobs.
      </div>
    );
  }

  return (
    <div
      role="region"
      aria-label="Background jobs"
      className="flex h-full min-h-0 flex-col overflow-hidden bg-surface-0 font-sans text-foreground dark:bg-neutral-950"
    >
      {/* Header bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-subtle bg-surface-1/70 px-3 py-2 text-xs backdrop-blur dark:bg-neutral-900/70">
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5 font-semibold text-[11px] text-foreground">
            <Briefcase size={13} className="text-primary" />
            <span>Background Queue</span>
          </div>
          {activeCount > 0 && (
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-cyan-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-cyan-500" />
            </span>
          )}
          <span className="text-[10px] text-muted-foreground">
            ({jobs.length} total · {activeCount} active)
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          {/* Status Filter Chips */}
          <div className="flex items-center rounded-lg border border-border-subtle bg-surface-2/60 p-0.5 text-[10px]">
            {(["all", "running", "queued", "completed", "failed"] as const).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setStatusFilter(s)}
                className={`rounded px-1.5 py-0.5 capitalize transition ${
                  statusFilter === s
                    ? "bg-primary text-primary-foreground font-semibold shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {s}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={load}
            disabled={loading}
            aria-label="Refresh jobs"
            title="Refresh jobs"
            className="rounded-md border border-border-subtle bg-surface-2/60 p-1 text-muted-foreground hover:bg-surface-2 hover:text-foreground transition"
          >
            <RefreshCw size={12} className={loading ? "animate-spin text-primary" : ""} />
          </button>
        </div>
      </div>

      {/* Body List */}
      <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto p-3">
        {loading && jobs.length === 0 ? (
          <div className="flex h-48 items-center justify-center gap-2 text-xs text-muted-foreground">
            <Loader2 size={16} className="animate-spin text-primary" />
            <span>Checking background worker tasks…</span>
          </div>
        ) : error ? (
          <div className="rounded-lg border border-rose-300/20 bg-rose-500/10 p-3 text-xs text-rose-200">
            {error}
          </div>
        ) : filteredJobs.length === 0 ? (
          <div className="flex h-48 flex-col items-center justify-center gap-1 text-center">
            <Clock size={24} className="text-muted-foreground/40" />
            <p className="text-xs font-semibold text-foreground">No Background Jobs Found</p>
            <p className="text-[11px] text-muted-foreground">
              {statusFilter === "all"
                ? "Queued prompts, long builds, and media generation will appear here."
                : `No jobs currently matching status "${statusFilter}".`}
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {filteredJobs.map((job) => {
              const isExpanded = expandedId === job.job_id;
              const Icon = job.kind === "turn" ? Briefcase : FileCode;
              const StatusIcon = STATUS_ICON[job.status];
              const durationStr = formatDuration(job.started_at, job.completed_at);

              return (
                <div
                  key={job.job_id}
                  className={`rounded-lg border transition ${
                    job.status === "running"
                      ? "border-cyan-500/50 bg-cyan-500/5 shadow-xs shadow-cyan-500/10"
                      : "border-border-subtle/80 bg-surface-1/40 hover:bg-surface-1/70 dark:bg-neutral-900/40"
                  }`}
                >
                  {/* Summary Bar */}
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setExpandedId(isExpanded ? null : job.job_id)}
                    className="flex items-center justify-between gap-2.5 p-2.5 cursor-pointer text-left"
                  >
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      <div className="rounded-md bg-surface-2 p-1 text-muted-foreground">
                        <Icon size={13} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-[11px] font-semibold text-foreground truncate">
                            {KIND_LABEL[job.kind]}
                          </span>
                          <span className="font-mono text-[10px] text-muted-foreground/70 truncate max-w-[140px]">
                            {job.job_id.slice(0, 8)}…
                          </span>
                        </div>
                        <p className="text-[10px] text-muted-foreground truncate">
                          {job.label}
                        </p>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      {durationStr && (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          {durationStr}
                        </span>
                      )}
                      <span
                        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-semibold capitalize ${STATUS_COLOR[job.status]}`}
                      >
                        <StatusIcon
                          size={10}
                          className={job.status === "running" ? "animate-spin" : ""}
                        />
                        <span>{job.status}</span>
                      </span>
                      <div className="text-muted-foreground/60">
                        {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </div>
                    </div>
                  </div>

                  {/* Expanded Diagnostics Drawer */}
                  {isExpanded && (
                    <div className="border-t border-border-subtle/70 bg-surface-0/60 p-3 text-xs dark:bg-neutral-950/60">
                      <div className="flex items-center justify-between pb-2 mb-2 border-b border-border-subtle/50 text-[10px] text-muted-foreground">
                        <span className="font-mono">{job.job_id}</span>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            copyToClipboard(job.job_id, job.job_id);
                          }}
                          className="inline-flex items-center gap-1 rounded bg-surface-2 px-1.5 py-0.5 text-[10px] hover:text-foreground transition"
                        >
                          {copiedId === job.job_id ? (
                            <>
                              <Check size={10} className="text-emerald-400" />
                              <span>Copied</span>
                            </>
                          ) : (
                            <>
                              <Copy size={10} />
                              <span>Copy ID</span>
                            </>
                          )}
                        </button>
                      </div>

                      <div className="grid grid-cols-2 gap-2 text-[10px] mb-2 text-muted-foreground">
                        {job.created_at && (
                          <div>
                            <span className="block font-semibold uppercase text-[9px]">Queued At:</span>
                            <span className="text-foreground">{new Date(job.created_at).toLocaleString()}</span>
                          </div>
                        )}
                        {job.started_at && (
                          <div>
                            <span className="block font-semibold uppercase text-[9px]">Started At:</span>
                            <span className="text-foreground">{new Date(job.started_at).toLocaleString()}</span>
                          </div>
                        )}
                        {job.completed_at && (
                          <div>
                            <span className="block font-semibold uppercase text-[9px]">Completed At:</span>
                            <span className="text-foreground">{new Date(job.completed_at).toLocaleString()}</span>
                          </div>
                        )}
                        {job.priority !== undefined && (
                          <div>
                            <span className="block font-semibold uppercase text-[9px]">Priority:</span>
                            <span className="text-foreground">{job.priority}</span>
                          </div>
                        )}
                        {job.format && (
                          <div>
                            <span className="block font-semibold uppercase text-[9px]">Format:</span>
                            <span className="text-foreground">{job.format}</span>
                          </div>
                        )}
                      </div>

                      {job.error && (
                        <div className="rounded border border-rose-500/30 bg-rose-500/10 p-2 font-mono text-[10px] text-rose-300">
                          {job.error}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export default DeepSpaceJobsPanel;

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, ChevronRight, Clock, Copy, FileText, GitBranch, X } from "lucide-react";
import { fetchWithAuth } from "@/lib/api";

interface WorkspaceChangeEntry {
  path: string;
  status: "added" | "modified" | "deleted" | "renamed";
  lines_added: number;
  lines_removed: number;
  binary?: boolean;
  oversized?: boolean;
}

interface WorkspaceChangesResponse {
  entries: WorkspaceChangeEntry[];
  total_files: number;
  total_lines_added: number;
  total_lines_removed: number;
}

interface WorkspaceChangeDiffResponse {
  diff: string;
  file_path: string;
  lines_added: number;
  lines_removed: number;
}

interface DeepSpaceWorkspaceChangesDiffViewerProps {
  isOpen: boolean;
  onClose: () => void;
  conversationId: string | null;
  turn: number;
}

function parseDiff(diff: string): Array<{
  type: "header" | "added" | "removed" | "unchanged" | "blank";
  content: string;
  oldLineNumber?: number;
  newLineNumber?: number;
}> {
  const lines: Array<{
    type: "header" | "added" | "removed" | "unchanged" | "blank";
    content: string;
    oldLineNumber?: number;
    newLineNumber?: number;
  }> = [];

  let oldLine = 0;
  let newLine = 0;

  for (const line of diff.split("\n")) {
    if (
      line.startsWith("diff --git") ||
      line.startsWith("index ") ||
      line.startsWith("--- ") ||
      line.startsWith("+++ ") ||
      line.startsWith("@@")
    ) {
      lines.push({ type: "header", content: line });
      const hunkMatch = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)/);
      if (hunkMatch) {
        oldLine = parseInt(hunkMatch[1], 10) - 1;
        newLine = parseInt(hunkMatch[2], 10) - 1;
      }
      continue;
    }

    if (line.startsWith("+") && !line.startsWith("+++")) {
      newLine += 1;
      lines.push({
        type: "added",
        content: line.slice(1),
        oldLineNumber: undefined,
        newLineNumber: newLine,
      });
      continue;
    }

    if (line.startsWith("-") && !line.startsWith("---")) {
      oldLine += 1;
      lines.push({
        type: "removed",
        content: line.slice(1),
        oldLineNumber: oldLine,
        newLineNumber: undefined,
      });
      continue;
    }

    if (line.startsWith(" ")) {
      oldLine += 1;
      newLine += 1;
      lines.push({
        type: "unchanged",
        content: line.slice(1),
        oldLineNumber: oldLine,
        newLineNumber: newLine,
      });
      continue;
    }

    if (line === "") {
      lines.push({ type: "blank", content: "" });
      continue;
    }

    lines.push({ type: "unchanged", content: line });
  }

  return lines;
}

export default function DeepSpaceWorkspaceChangesDiffViewer({
  isOpen,
  onClose,
  conversationId,
  turn,
}: DeepSpaceWorkspaceChangesDiffViewerProps) {
  const [entries, setEntries] = useState<WorkspaceChangeEntry[]>([]);
  const [loadingEntries, setLoadingEntries] = useState(true);
  const [selectedFileIndex, setSelectedFileIndex] = useState<number | null>(null);
  const [diff, setDiff] = useState<string>("");
  const [loadingDiff, setLoadingDiff] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const fetchEntries = useCallback(async () => {
    if (!conversationId) return;
    setLoadingEntries(true);
    setError(null);
    // Reset diff view when fetching new entries (turn changed)
    setDiff("");
    setSelectedFileIndex(null);
    try {
      const response = await fetchWithAuth(
        `/host/chats/${encodeURIComponent(conversationId)}/workspace-changes?turn=${turn}`,
      );
      if (!response.ok) {
        throw new Error(`Failed to load workspace changes: ${response.status}`);
      }
      const data: WorkspaceChangesResponse = await response.json();
      if (mountedRef.current) {
        setEntries(data.entries);
        setLoadingEntries(false);
      }
    } catch (cause) {
      if (mountedRef.current) {
        setError(cause instanceof Error ? cause.message : "Failed to load workspace changes.");
        setLoadingEntries(false);
      }
    }
  }, [conversationId, turn]);

  const fetchDiff = useCallback(
    async (fileIndex: number) => {
      if (!conversationId) return;
      setLoadingDiff(true);
      setSelectedFileIndex(fileIndex);
      try {
        const response = await fetchWithAuth(
          `/host/chats/${encodeURIComponent(conversationId)}/workspace-changes/${turn}/diff/${fileIndex}`,
        );
        if (!response.ok) {
          throw new Error(`Failed to load diff: ${response.status}`);
        }
        const data: WorkspaceChangeDiffResponse = await response.json();
        if (mountedRef.current) {
          setDiff(data.diff);
          setLoadingDiff(false);
        }
      } catch (cause) {
        if (mountedRef.current) {
          setError(cause instanceof Error ? cause.message : "Failed to load file diff.");
          setLoadingDiff(false);
        }
      }
    },
    [conversationId, turn],
  );

  useEffect(() => {
    mountedRef.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchEntries();
    return () => {
      mountedRef.current = false;
    };
  }, [fetchEntries]);

  if (!isOpen) return null;

  const statusIcon = (status: string) => {
    switch (status) {
      case "added":
        return <GitBranch size={13} className="text-emerald-400" />;
      case "modified":
        return <GitBranch size={13} className="text-amber-400" />;
      case "deleted":
        return <GitBranch size={13} className="text-rose-400" />;
      case "renamed":
        return <FileText size={13} className="text-violet-400" />;
      default:
        return <FileText size={13} className="text-muted-foreground" />;
    }
  };

  const statusColor = (status: string) => {
    switch (status) {
      case "added":
        return "text-emerald-400";
      case "modified":
        return "text-amber-400";
      case "deleted":
        return "text-rose-400";
      case "renamed":
        return "text-violet-400";
      default:
        return "text-muted-foreground";
    }
  };

  const menuContent = (
    <div
      className="deepspace-workspace-diff-viewer bg-surface-0 fixed inset-0 z-[1000] flex flex-col dark:bg-neutral-950"
      role="dialog"
      aria-modal="true"
      aria-label="Workspace Changes"
    >
      {/* Header */}
      <div className="border-border-subtle bg-surface-1/70 flex items-center justify-between gap-2 border-b px-3 py-2 text-xs backdrop-blur dark:border-neutral-900/70 dark:bg-neutral-900/70">
        <div className="flex items-center gap-2">
          <GitBranch size={13} className="text-primary" />
          <span className="text-foreground font-semibold">Turn {turn} — Workspace Changes</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onClose}
            aria-label="Close workspace changes"
            className="text-muted-foreground hover:text-foreground rounded-md p-1.5 transition dark:hover:bg-neutral-800"
          >
            <X size={14} />
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left: File list */}
        <div className="border-border-subtle bg-surface-1/50 flex w-[45vw] max-w-md min-w-0 flex-col border-r dark:border-neutral-900/20">
          <div className="border-border-subtle/50 border-b px-3 py-1.5 dark:border-neutral-900/10">
            <span className="text-muted-foreground text-[10px] font-bold tracking-[0.16em] uppercase">
              Files ({entries.length})
            </span>
          </div>
          {loadingEntries ? (
            <div className="text-muted-foreground flex items-center justify-center py-8 text-[11px]">
              <Clock size={14} className="mr-1.5 animate-spin" />
              Loading changes…
            </div>
          ) : error ? (
            <div className="rounded-lg border border-rose-300/15 bg-rose-500/10 p-3 text-[11px] text-rose-200">
              {error}
            </div>
          ) : entries.length === 0 ? (
            <div className="text-muted-foreground flex items-center justify-center py-8 text-[11px]">
              No workspace changes recorded for this turn.
            </div>
          ) : (
            <div className="custom-scrollbar flex-1 space-y-0.5 overflow-y-auto">
              {entries.map((entry, index) => {
                const isSelected = selectedFileIndex === index;
                const isBinaryOrOversized = entry.binary || entry.oversized;
                return (
                  <button
                    key={`${entry.path}-${index}`}
                    type="button"
                    onClick={() => {
                      if (!isBinaryOrOversized) {
                        fetchDiff(index);
                      }
                    }}
                    disabled={isBinaryOrOversized}
                    className={`flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 text-left transition ${
                      isSelected
                        ? "bg-primary/10 border-primary text-primary dark:bg-primary/10 dark:text-primary"
                        : "text-foreground/70 hover:bg-surface-2 hover:border-border-subtle border-transparent"
                    }`}
                    title={
                      isBinaryOrOversized
                        ? "Binary or oversized file — diff not available"
                        : `View diff for ${entry.path}`
                    }
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        {statusIcon(entry.status)}
                        <span
                          className={`truncate font-mono text-[11px] ${statusColor(entry.status)}`}
                        >
                          {entry.path}
                        </span>
                        {isBinaryOrOversized && (
                          <span className="text-muted-foreground/50 text-[9px] tracking-wider uppercase">
                            {entry.binary ? "binary" : "oversized"}
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 flex items-center gap-1.5 text-[9px]">
                        <span
                          className={`font-mono ${entry.lines_added > 0 ? "text-emerald-400" : "text-muted-foreground/50"}`}
                        >
                          +{entry.lines_added}
                        </span>
                        <span className="text-muted-foreground/30">•</span>
                        <span
                          className={`font-mono ${entry.lines_removed > 0 ? "text-rose-400" : "text-muted-foreground/50"}`}
                        >
                          -{entry.lines_removed}
                        </span>
                      </div>
                    </div>
                    <ChevronRight
                      size={11}
                      className={`shrink-0 ${isSelected ? "text-primary" : "text-muted-foreground/40"}`}
                    />
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Right: Diff view */}
        <div className="bg-surface-0 flex min-w-0 flex-1 flex-col dark:bg-neutral-950">
          {selectedFileIndex === null || loadingDiff ? (
            <div className="text-muted-foreground flex h-full flex-col items-center justify-center text-[11px]">
              {loadingDiff ? (
                <>
                  <Clock size={16} className="mb-2 animate-spin" />
                  <span>Loading diff…</span>
                </>
              ) : (
                <>
                  <FileText size={24} className="mb-2 opacity-20" />
                  <span>Select a file to view its diff</span>
                </>
              )}
            </div>
          ) : error ? (
            <div className="flex h-full flex-col items-center justify-center p-6 text-[11px] text-rose-200">
              <AlertTriangle size={24} className="mb-2 text-rose-400" />
              <span>{error}</span>
            </div>
          ) : (
            <div className="custom-scrollbar flex-1 overflow-auto p-4">
              <div className="border-border-subtle bg-surface-1/40 overflow-hidden rounded-lg border dark:border-neutral-900/20">
                <div className="border-border-subtle/50 bg-surface-2/30 flex items-center justify-between border-b px-3 py-1.5 dark:bg-neutral-900/20">
                  <span className="text-muted-foreground max-w-[60vw] truncate font-mono text-[10px]">
                    {entries[selectedFileIndex]?.path ?? ""}
                  </span>
                  <div className="flex items-center gap-1.5 font-mono text-[10px]">
                    <span className="text-emerald-400">
                      +{entries[selectedFileIndex]?.lines_added ?? 0}
                    </span>
                    <span className="text-muted-foreground/50">•</span>
                    <span className="text-rose-400">
                      -{entries[selectedFileIndex]?.lines_removed ?? 0}
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        navigator.clipboard.writeText(diff);
                        onClose();
                      }}
                      className="text-muted-foreground hover:text-foreground p-1 transition"
                      title="Copy diff to clipboard"
                    >
                      <Copy size={12} />
                    </button>
                  </div>
                </div>
                <pre className="overflow-auto p-3 font-mono text-[10px] leading-tight whitespace-pre-wrap">
                  {parseDiff(diff).map((line, i) => {
                    const oldNum =
                      line.oldLineNumber !== undefined
                        ? String(line.oldLineNumber).padStart(4, " ")
                        : "     ";
                    const newNum =
                      line.newLineNumber !== undefined
                        ? String(line.newLineNumber).padStart(4, " ")
                        : "     ";
                    const baseClass = `flex w-full min-w-[8ch] text-[10px] font-mono leading-none`;
                    switch (line.type) {
                      case "added":
                        return (
                          <div
                            key={i}
                            className={`${baseClass} border-l-2 border-l-emerald-400 bg-emerald-400/10 pl-1`}
                          >
                            <span className="inline-block w-20 text-emerald-300 select-none">
                              {newNum}
                            </span>
                            <span className="ml-1">{line.content}</span>
                          </div>
                        );
                      case "removed":
                        return (
                          <div
                            key={i}
                            className={`${baseClass} border-l-2 border-l-rose-400 bg-rose-400/10 pl-1`}
                          >
                            <span className="inline-block w-20 text-rose-300 select-none">
                              {oldNum}
                            </span>
                            <span className="ml-1">{line.content}</span>
                          </div>
                        );
                      case "unchanged":
                        return (
                          <div key={i} className={`${baseClass} text-muted-foreground/50`}>
                            <span className="text-muted-foreground/30 inline-block w-20 select-none">
                              {oldNum} {newNum}
                            </span>
                            <span className="ml-1">{line.content}</span>
                          </div>
                        );
                      case "header":
                        return (
                          <div key={i} className={`${baseClass} pl-1 font-bold text-violet-400`}>
                            {line.content}
                          </div>
                        );
                      default:
                        return (
                          <div key={i} className={`${baseClass} text-muted-foreground/30 pl-1`}>
                            {line.content}
                          </div>
                        );
                    }
                  })}
                </pre>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );

  return createPortal(menuContent, document.body);
}

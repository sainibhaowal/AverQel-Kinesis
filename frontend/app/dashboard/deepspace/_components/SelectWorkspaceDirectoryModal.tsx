"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronRight, Folder, FolderOpen, FolderPlus, Loader2, Plus, X } from "lucide-react";

import {
  browseDirectory,
  createDirectory,
  type DirectoryEntry,
  type DirectoryListing,
} from "../_lib/workspace-api";

export interface SelectWorkspaceDirectoryModalProps {
  open: boolean;
  onClose: () => void;
  onSelect: (path: string, name: string) => void;
  busy?: boolean;
}

function getSeparator(path: string): string {
  return path.includes("\\") && !path.includes("/") ? "\\" : "/";
}

function getDirectoryBaseName(path: string): string {
  const normalized = path.replace(/[/\\]+$/, "");
  const parts = normalized.split(/[/\\]/);
  return parts[parts.length - 1] || path;
}

export default function SelectWorkspaceDirectoryModal({
  open,
  onClose,
  onSelect,
  busy = false,
}: SelectWorkspaceDirectoryModalProps) {
  const [currentLevel, setCurrentLevel] = useState<DirectoryListing | null>(null);
  const [selectedEntry, setSelectedEntry] = useState<DirectoryEntry | null>(null);
  const [childLevel, setChildLevel] = useState<DirectoryListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);

  // New folder state
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [inFlightCreate, setInFlightCreate] = useState(false);

  // Path input editing
  const [isEditingPath, setIsEditingPath] = useState(false);
  const [pathDraft, setPathDraft] = useState("");

  const requestSeq = useRef(0);
  const abortControllerRef = useRef<AbortController | null>(null);

  const fetchLevel = useCallback(
    async (path?: string, isChild = false) => {
      abortControllerRef.current?.abort();
      const controller = new AbortController();
      abortControllerRef.current = controller;
      const seq = ++requestSeq.current;

      setLoading(true);
      setError(null);

      try {
        const listing = await browseDirectory(path, controller.signal);
        if (seq !== requestSeq.current) return;
        if (isChild) {
          setChildLevel(listing);
        } else {
          setCurrentLevel(listing);
          setSelectedEntry(null);
          setChildLevel(null);
          setPathDraft(listing.path);
        }
      } catch (err: unknown) {
        if (seq !== requestSeq.current) return;
        if (err instanceof Error && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (seq === requestSeq.current) {
          setLoading(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    if (open) {
      setSelectedEntry(null);
      setChildLevel(null);
      setCreatingFolder(false);
      setNewFolderName("");
      setCreateError(null);
      setIsEditingPath(false);
      void fetchLevel();
    } else {
      abortControllerRef.current?.abort();
    }
  }, [open, fetchLevel]);

  // Click row in left column
  const handleSelectRow = (entry: DirectoryEntry) => {
    setSelectedEntry(entry);
    setChildLevel(null);
    void fetchLevel(entry.path, true);
  };

  // Click row in right column (descend)
  const handleAdvanceRow = (entry: DirectoryEntry) => {
    if (childLevel) {
      setCurrentLevel(childLevel);
      handleSelectRow(entry);
    }
  };

  // Target directory determined by editing draft, selection, or current level
  const resolvedDraftPath = isEditingPath && pathDraft.trim() ? pathDraft.trim() : null;
  const effectiveTargetPath = resolvedDraftPath ?? selectedEntry?.path ?? currentLevel?.path ?? "";
  const effectiveTargetName =
    selectedEntry?.name ?? (effectiveTargetPath ? getDirectoryBaseName(effectiveTargetPath) : "");

  const handleOpen = () => {
    if (!effectiveTargetPath || busy || inFlightCreate) return;
    const name = effectiveTargetName || getDirectoryBaseName(effectiveTargetPath);
    onSelect(effectiveTargetPath, name);
    onClose();
  };

  const handleCreateNewFolder = async () => {
    const trimmed = newFolderName.trim();
    if (!trimmed || inFlightCreate) return;
    const parentPath = effectiveTargetPath;
    if (!parentPath) return;

    setInFlightCreate(true);
    setCreateError(null);
    try {
      const createdPath = await createDirectory(parentPath, trimmed);
      setCreatingFolder(false);
      setNewFolderName("");
      // Refresh listing and select newly created folder
      await fetchLevel(parentPath, false);
      setSelectedEntry({ name: trimmed, path: createdPath, hidden: trimmed.startsWith(".") });
      void fetchLevel(createdPath, true);
    } catch (err: unknown) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setInFlightCreate(false);
    }
  };

  if (!open) return null;

  const visibleEntries = (entries: DirectoryEntry[]) =>
    entries.filter((entry) => showHidden || !entry.hidden);

  const leftEntries = currentLevel ? visibleEntries(currentLevel.entries) : [];
  const rightEntries = childLevel ? visibleEntries(childLevel.entries) : [];

  const modalContent = (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Select Workspace Directory"
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/80 p-4 backdrop-blur-md"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className="border border-white/10 bg-[#161a18] text-[#f1f5f9] flex h-[500px] w-full max-w-[680px] flex-col rounded-2xl shadow-[0_25px_60px_-15px_rgba(0,0,0,0.9)] overflow-hidden">
        {/* Header */}
        <div className="border-white/10 flex flex-col gap-2 border-b bg-[#121614] px-5 py-3.5">
          <div className="flex items-center justify-between">
            <h2 className="text-[15px] font-semibold text-white/90">Select Workspace Directory</h2>
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="text-white/40 hover:text-white/80 rounded-lg p-1 transition"
            >
              <X size={16} />
            </button>
          </div>

          {/* Breadcrumb / Path Edit Bar */}
          <div className="flex items-center gap-1.5 text-xs text-white/60">
            {isEditingPath ? (
              <form
                className="flex w-full items-center gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (pathDraft.trim()) {
                    setIsEditingPath(false);
                    void fetchLevel(pathDraft.trim());
                  }
                }}
              >
                <input
                  type="text"
                  autoFocus
                  value={pathDraft}
                  onChange={(e) => setPathDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      setIsEditingPath(false);
                      if (currentLevel) setPathDraft(currentLevel.path);
                    }
                  }}
                  className="border-white/20 bg-white/5 text-foreground w-full rounded-md border px-2.5 py-1 text-xs outline-none focus:border-white/40"
                />
                <button
                  type="submit"
                  className="bg-white/10 hover:bg-white/20 text-white rounded px-2 py-1 text-xs font-medium"
                >
                  Go
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setIsEditingPath(false);
                    if (currentLevel) setPathDraft(currentLevel.path);
                  }}
                  className="text-white/60 hover:text-white px-1 text-xs"
                >
                  Cancel
                </button>
              </form>
            ) : (
              <div className="flex w-full items-center justify-between">
                <div className="flex flex-wrap items-center gap-1 overflow-x-auto text-[13px] font-medium text-white/80">
                  {currentLevel?.crumbs && currentLevel.crumbs.length > 0 ? (
                    currentLevel.crumbs.map((crumb, idx) => {
                      const isLast = idx === currentLevel.crumbs.length - 1;
                      const displayName =
                        crumb.name === currentLevel.home
                          ? "Home"
                          : crumb.name || getSeparator(crumb.path);
                      return (
                        <div key={crumb.path} className="flex items-center gap-1">
                          {idx > 0 && <ChevronRight size={12} className="text-white/30" />}
                          <button
                            type="button"
                            onClick={() => void fetchLevel(crumb.path)}
                            className={`rounded px-1.5 py-0.5 transition ${
                              isLast
                                ? "bg-white/10 font-semibold text-white"
                                : "text-white/60 hover:bg-white/5 hover:text-white"
                            }`}
                          >
                            {displayName}
                          </button>
                        </div>
                      );
                    })
                  ) : (
                    <span>Home</span>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => setIsEditingPath(true)}
                  title="Edit path directly"
                  className="text-white/40 hover:text-white/80 rounded p-1 transition"
                >
                  <span className="text-[11px] underline">Edit</span>
                </button>
              </div>
            )}
          </div>
        </div>

        {/* Content (Miller columns) */}
        <div className="relative flex flex-1 min-h-0 overflow-hidden">
          {loading && (
            <div className="absolute right-4 bottom-2 z-10 flex items-center gap-1.5 rounded-full bg-black/60 px-2.5 py-1 text-[11px] text-white/80 backdrop-blur">
              <Loader2 size={12} className="animate-spin text-emerald-400" />
              <span>Loading...</span>
            </div>
          )}

          {error ? (
            <div className="flex flex-1 items-center justify-center p-4 text-center text-xs text-rose-400">
              {error}
            </div>
          ) : (
            <div className="flex h-full w-full divide-x divide-white/10 overflow-hidden">
              {/* Left Column (Current Level) */}
              <div className="flex-1 overflow-y-auto p-2">
                {leftEntries.length === 0 ? (
                  <div className="p-4 text-center text-xs text-white/40">No subdirectories</div>
                ) : (
                  leftEntries.map((entry) => {
                    const isSelected = selectedEntry?.path === entry.path;
                    return (
                      <button
                        key={entry.path}
                        type="button"
                        onClick={() => handleSelectRow(entry)}
                        className={`flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-xs transition ${
                          isSelected
                            ? "bg-white/15 font-medium text-white"
                            : "text-white/70 hover:bg-white/5 hover:text-white"
                        }`}
                      >
                        <div className="flex min-w-0 items-center gap-2">
                          {isSelected ? (
                            <FolderOpen size={15} className="text-emerald-400 shrink-0" />
                          ) : (
                            <Folder size={15} className="text-white/50 shrink-0" />
                          )}
                          <span className="truncate">{entry.name}</span>
                        </div>
                        <ChevronRight size={13} className="text-white/30 shrink-0" />
                      </button>
                    );
                  })
                )}
              </div>

              {/* Right Column (Children of selected folder, if any) */}
              {selectedEntry && (
                <div className="flex-1 overflow-y-auto p-2 bg-white/[0.02]">
                  {rightEntries.length === 0 ? (
                    <div className="p-4 text-center text-xs text-white/40">Empty folder</div>
                  ) : (
                    rightEntries.map((entry) => (
                      <button
                        key={entry.path}
                        type="button"
                        onClick={() => handleAdvanceRow(entry)}
                        className="flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-xs text-white/70 transition hover:bg-white/5 hover:text-white"
                      >
                        <div className="flex min-w-0 items-center gap-2">
                          <Folder size={15} className="text-white/50 shrink-0" />
                          <span className="truncate">{entry.name}</span>
                        </div>
                        <ChevronRight size={13} className="text-white/30 shrink-0" />
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Create Folder Modal Overlay */}
        {creatingFolder && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
            <div className="border-white/10 bg-[#202024] text-foreground flex w-full max-w-sm flex-col gap-3 rounded-xl border p-4 shadow-xl">
              <div className="flex items-center gap-2 text-sm font-semibold text-white/90">
                <FolderPlus size={16} className="text-emerald-400" />
                <span>Create New Folder</span>
              </div>
              <p className="text-[11px] text-white/50 truncate">
                Inside: {effectiveTargetPath}
              </p>
              <input
                type="text"
                autoFocus
                placeholder="Folder name"
                value={newFolderName}
                onChange={(e) => setNewFolderName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void handleCreateNewFolder();
                  if (e.key === "Escape") setCreatingFolder(false);
                }}
                className="border-white/15 bg-white/5 text-foreground w-full rounded-lg border px-3 py-2 text-xs outline-none focus:border-emerald-400/60"
              />
              {createError && <p className="text-[11px] text-rose-400">{createError}</p>}
              <div className="mt-1 flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setCreatingFolder(false)}
                  disabled={inFlightCreate}
                  className="rounded-lg px-3 py-1.5 text-xs font-medium text-white/60 hover:text-white"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void handleCreateNewFolder()}
                  disabled={!newFolderName.trim() || inFlightCreate}
                  className="bg-emerald-500 hover:bg-emerald-400 inline-flex items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-xs font-semibold text-black transition disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {inFlightCreate && <Loader2 size={12} className="animate-spin" />}
                  Create
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="border-white/10 flex items-center justify-between border-t bg-[#121614] px-5 py-3">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => {
                setCreatingFolder(true);
                setNewFolderName("");
                setCreateError(null);
              }}
              disabled={!effectiveTargetPath || busy}
              className="border-white/15 bg-white/5 text-white/90 hover:bg-white/10 hover:text-white flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-semibold transition active:scale-95 disabled:opacity-40"
            >
              <Plus size={13} />
              <span>New folder</span>
            </button>
            <button
              type="button"
              onClick={() => setShowHidden((prev) => !prev)}
              className="text-white/50 hover:text-white/80 flex items-center gap-1.5 text-xs transition"
            >
              <span
                className={`flex h-3.5 w-3.5 items-center justify-center rounded border ${
                  showHidden ? "border-emerald-400 bg-emerald-500/20 text-emerald-400" : "border-white/30"
                }`}
              >
                {showHidden && <Check size={10} />}
              </span>
              <span>Show hidden files</span>
            </button>
          </div>

          <div className="mx-3 hidden min-w-0 flex-1 truncate text-right text-[11px] text-white/50 sm:block">
            <span className="text-white/30 mr-1.5">Target:</span>
            <span className="text-emerald-400 font-mono" title={effectiveTargetPath || "None"}>
              {effectiveTargetPath || "None selected"}
            </span>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="border-white/10 bg-white/5 text-white/80 hover:bg-white/10 hover:text-white rounded-lg border px-4 py-1.5 text-xs font-semibold transition"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleOpen}
              disabled={!effectiveTargetPath || busy}
              className="bg-white text-black hover:bg-white/90 rounded-lg px-5 py-1.5 text-xs font-semibold shadow transition disabled:cursor-not-allowed disabled:opacity-40"
            >
              Open
            </button>
          </div>
        </div>
      </div>
    </div>
  );

  if (typeof document !== "undefined") {
    return createPortal(modalContent, document.body);
  }
  return modalContent;
}

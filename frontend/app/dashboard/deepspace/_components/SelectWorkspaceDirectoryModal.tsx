"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronRight, Folder, FolderOpen, FolderPlus, Plus, X } from "lucide-react";

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

  // Opening the modal starts a fresh directory selection; its previous
  // selection and folder-creation form must not leak into the next open.
  /* eslint-disable react-hooks/set-state-in-effect */
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
  /* eslint-enable react-hooks/set-state-in-effect */

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
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-sm dark:bg-black/60"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className="border-border bg-card text-card-foreground flex h-[min(500px,calc(100dvh-2rem))] w-full max-w-[680px] flex-col overflow-hidden rounded-2xl border shadow-2xl">
        {/* Header */}
        <div className="border-border bg-surface-1 flex flex-col gap-2 border-b px-5 py-3.5">
          <div className="flex items-center justify-between">
            <h2 className="text-foreground text-base font-semibold">Select Workspace Directory</h2>
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              aria-label="Close workspace picker"
              className="text-muted-foreground hover:bg-muted hover:text-foreground rounded-lg p-1 transition-colors"
            >
              <X size={16} />
            </button>
          </div>

          {/* Breadcrumb / Path Edit Bar */}
          <div className="text-muted-foreground flex items-center gap-1.5 text-[13px]">
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
                  aria-label="Workspace directory path"
                  className="border-border bg-background text-foreground placeholder:text-muted-foreground w-full rounded-lg border px-2.5 py-1.5 text-[13px] outline-none focus:border-primary"
                />
                <button
                  type="submit"
                  className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-3 py-1.5 text-xs font-semibold transition-colors"
                >
                  Go
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setIsEditingPath(false);
                    if (currentLevel) setPathDraft(currentLevel.path);
                  }}
                  className="text-muted-foreground hover:bg-muted hover:text-foreground rounded-md px-2 py-1 text-xs transition-colors"
                >
                  Cancel
                </button>
              </form>
            ) : (
              <div className="flex w-full items-center justify-between">
                <div className="flex flex-wrap items-center gap-1 overflow-x-auto text-[13px] font-medium text-foreground">
                  {currentLevel?.crumbs && currentLevel.crumbs.length > 0 ? (
                    currentLevel.crumbs.map((crumb, idx) => {
                      const isLast = idx === currentLevel.crumbs.length - 1;
                      const displayName =
                        crumb.name === currentLevel.home
                          ? "Home"
                          : crumb.name || getSeparator(crumb.path);
                      return (
                        <div key={crumb.path} className="flex items-center gap-1">
                          {idx > 0 && <ChevronRight size={12} className="text-muted-foreground/70" />}
                          <button
                            type="button"
                            onClick={() => void fetchLevel(crumb.path)}
                            className={`rounded px-1.5 py-0.5 transition ${
                              isLast
                                ? "bg-primary/10 font-semibold text-primary"
                                : "text-muted-foreground hover:bg-muted hover:text-foreground"
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
                  className="text-muted-foreground hover:bg-muted hover:text-foreground rounded-md p-1 transition-colors"
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
            <div className="border-border bg-card text-foreground absolute right-4 bottom-2 z-10 flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs shadow-md">
              <AverQelLoader size={12} className="text-primary animate-spin" />
              <span>Loading...</span>
            </div>
          )}

          {error ? (
            <div className="flex flex-1 items-center justify-center p-4 text-center text-[13px] text-rose-700 dark:text-rose-300">
              {error}
            </div>
          ) : (
            <div className="flex h-full w-full divide-x divide-border overflow-hidden">
              {/* Left Column (Current Level) */}
              <div className="flex-1 overflow-y-auto p-2">
                {leftEntries.length === 0 ? (
                  <div className="text-muted-foreground p-4 text-center text-[13px]">No subdirectories</div>
                ) : (
                  leftEntries.map((entry) => {
                    const isSelected = selectedEntry?.path === entry.path;
                    return (
                      <button
                        key={entry.path}
                        type="button"
                        onClick={() => handleSelectRow(entry)}
                        className={`flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors ${
                          isSelected
                            ? "bg-primary/10 font-semibold text-foreground"
                            : "text-foreground hover:bg-muted"
                        }`}
                      >
                        <div className="flex min-w-0 items-center gap-2">
                          {isSelected ? (
                            <FolderOpen size={15} className="text-primary shrink-0" />
                          ) : (
                            <Folder size={15} className="text-muted-foreground shrink-0" />
                          )}
                          <span className="truncate">{entry.name}</span>
                        </div>
                        <ChevronRight size={13} className="text-muted-foreground shrink-0" />
                      </button>
                    );
                  })
                )}
              </div>

              {/* Right Column (Children of selected folder, if any) */}
              {selectedEntry && (
                <div className="bg-surface-1 flex-1 overflow-y-auto p-2">
                  {rightEntries.length === 0 ? (
                    <div className="text-muted-foreground p-4 text-center text-[13px]">Empty folder</div>
                  ) : (
                    rightEntries.map((entry) => (
                      <button
                        key={entry.path}
                        type="button"
                        onClick={() => handleAdvanceRow(entry)}
                        className="text-foreground hover:bg-muted flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors"
                      >
                        <div className="flex min-w-0 items-center gap-2">
                          <Folder size={15} className="text-muted-foreground shrink-0" />
                          <span className="truncate">{entry.name}</span>
                        </div>
                        <ChevronRight size={13} className="text-muted-foreground shrink-0" />
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
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-slate-950/40 p-4 backdrop-blur-sm dark:bg-black/60">
            <div className="border-border bg-card text-card-foreground flex w-full max-w-sm flex-col gap-3 rounded-2xl border p-5 shadow-2xl">
              <div className="text-foreground flex items-center gap-2 text-base font-semibold">
                <FolderPlus size={16} className="text-primary" />
                <span>Create New Folder</span>
              </div>
              <p className="text-muted-foreground truncate text-xs">
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
                className="border-border bg-background text-foreground placeholder:text-muted-foreground w-full rounded-lg border px-3 py-2 text-[13px] outline-none focus:border-primary"
              />
              {createError && <p className="text-xs text-rose-700 dark:text-rose-300">{createError}</p>}
              <div className="mt-1 flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setCreatingFolder(false)}
                  disabled={inFlightCreate}
                  className="text-muted-foreground hover:bg-muted hover:text-foreground rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void handleCreateNewFolder()}
                  disabled={!newFolderName.trim() || inFlightCreate}
                  className="bg-primary text-primary-foreground hover:bg-primary/90 inline-flex items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {inFlightCreate && <AverQelLoader size={12} className="animate-spin" />}
                  Create
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="border-border bg-surface-1 flex items-center justify-between border-t px-5 py-3">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => {
                setCreatingFolder(true);
                setNewFolderName("");
                setCreateError(null);
              }}
              disabled={!effectiveTargetPath || busy}
              className="border-border bg-background text-foreground hover:bg-muted flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-semibold transition-colors active:scale-95 disabled:opacity-40"
            >
              <Plus size={13} />
              <span>New folder</span>
            </button>
            <button
              type="button"
              onClick={() => setShowHidden((prev) => !prev)}
              className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-xs transition-colors"
            >
              <span
                className={`flex h-3.5 w-3.5 items-center justify-center rounded border ${
                  showHidden ? "border-primary bg-primary/10 text-primary" : "border-border"
                }`}
              >
                {showHidden && <Check size={10} />}
              </span>
              <span>Show hidden files</span>
            </button>
          </div>

          <div className="text-muted-foreground mx-3 hidden min-w-0 flex-1 truncate text-right text-xs sm:block">
            <span className="mr-1.5">Target:</span>
            <span className="text-primary font-mono" title={effectiveTargetPath || "None"}>
              {effectiveTargetPath || "None selected"}
            </span>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="border-border bg-background text-foreground hover:bg-muted rounded-lg border px-4 py-1.5 text-xs font-semibold transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleOpen}
              disabled={!effectiveTargetPath || busy}
              className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-lg px-5 py-1.5 text-xs font-semibold shadow-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40"
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

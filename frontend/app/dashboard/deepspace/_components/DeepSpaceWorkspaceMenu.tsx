"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, FolderPlus, FolderTree, Loader2, Trash2 } from "lucide-react";

import SelectWorkspaceDirectoryModal from "./SelectWorkspaceDirectoryModal";
import type { Workspace } from "../_lib/workspace-api";

export interface DeepSpaceWorkspaceMenuProps {
  /** The workspaces the user holds. */
  workspaces: Workspace[];
  /** The workspace the conversation uses. */
  workspace: Workspace | null;
  /** Called when the user picks another workspace. */
  onWorkspaceChange: (workspace: string) => void;
  /** Called when the user asks for a new workspace. */
  onCreateWorkspace: (name: string, path?: string) => void;
  /** Called when the user asks to delete a workspace. */
  onDeleteWorkspace?: (workspaceId: string) => void;
  /** Whether the menu can be opened. */
  disabled?: boolean;
  /** Whether a new workspace is being created. */
  creating?: boolean;
  /** The classes the trigger button uses. */
  triggerClassName?: string;
  onMouseEnter?: (event: React.MouseEvent<HTMLButtonElement>) => void;
  onMouseLeave?: (event: React.MouseEvent<HTMLButtonElement>) => void;
  onFocus?: (event: React.FocusEvent<HTMLButtonElement>) => void;
  onBlur?: (event: React.FocusEvent<HTMLButtonElement>) => void;
  "aria-describedby"?: string;
}

/**
 * The workspace menu.
 *
 * A conversation works in one workspace. The menu lists the workspaces the
 * user holds, marks the one in force, and can add another.
 */
export default function DeepSpaceWorkspaceMenu({
  workspaces,
  workspace,
  onWorkspaceChange,
  onCreateWorkspace,
  onDeleteWorkspace,
  disabled = false,
  creating = false,
  triggerClassName,
  onMouseEnter,
  onMouseLeave,
  onFocus,
  onBlur,
  "aria-describedby": ariaDescribedBy,
}: DeepSpaceWorkspaceMenuProps) {
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [showDirectoryPicker, setShowDirectoryPicker] = useState(false);
  const [name, setName] = useState("");
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
        setAdding(false);
        setName("");
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        setAdding(false);
        setName("");
      }
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const submit = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    onCreateWorkspace(trimmed);
    setName("");
    setAdding(false);
  };

  return (
    <div ref={containerRef} className="relative flex-shrink-0">
      <button
        type="button"
        aria-label="Workspace"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-describedby={ariaDescribedBy}
        disabled={disabled}
        onMouseEnter={onMouseEnter}
        onMouseLeave={onMouseLeave}
        onFocus={onFocus}
        onBlur={onBlur}
        onClick={() => {
          setOpen((value) => !value);
          setAdding(false);
          setName("");
        }}
        className={
          triggerClassName ??
          "border-glass-border bg-surface-2 text-foreground/60 hover:text-foreground flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold transition-all hover:bg-white/5 active:scale-95"
        }
      >
        <FolderTree size={11} />
        <span className="max-w-[10rem] truncate">{workspace?.name ?? "Workspace"}</span>
        <ChevronDown size={11} className={`transition-transform duration-200 ${open ? "rotate-180" : ""}`} />
      </button>

      {open ? (
        <div
          role="menu"
          aria-label="Workspace"
          className="border-glass-border bg-surface-2 absolute bottom-full left-0 z-[100] mb-2 max-h-[min(24rem,calc(100dvh-2rem))] w-[min(24rem,calc(100vw-2rem))] overflow-y-auto rounded-xl border p-1 shadow-xl"
        >
          {workspaces.map((entry) => {
            const selected = entry.id === workspace?.id;
            const isDefault = entry.id === "default" || entry.is_default;
            return (
              <div
                key={entry.id}
                className={`group/ws flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 transition-all ${
                  selected ? "bg-white/[0.08]" : "hover:bg-white/[0.04]"
                }`}
              >
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={selected}
                  onClick={() => {
                    setOpen(false);
                    onWorkspaceChange(entry.id);
                  }}
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                >
                  <span className="w-3 shrink-0">
                    {selected ? <Check size={12} className="text-emerald-400" /> : null}
                  </span>
                  <span className="flex min-w-0 flex-col">
                    <span className="text-foreground/90 truncate text-xs font-semibold">
                      {entry.name}
                    </span>
                    <span className="text-muted-foreground truncate text-[10px]" title={entry.path}>
                      {entry.path}
                    </span>
                  </span>
                </button>
                {!isDefault && onDeleteWorkspace ? (
                  <button
                    type="button"
                    aria-label={`Remove workspace ${entry.name}`}
                    title={`Unlink ${entry.name} from DeepSpace (does not delete real files)`}
                    onClick={(event) => {
                      event.stopPropagation();
                      onDeleteWorkspace(entry.id);
                    }}
                    className="text-muted-foreground hover:text-red-400 opacity-60 hover:opacity-100 transition-opacity p-1 ml-1 rounded hover:bg-white/10"
                  >
                    <Trash2 size={12} />
                  </button>
                ) : null}
              </div>
            );
          })}

          <div className="border-white/8 mt-1 border-t pt-1">
            {adding ? (
              <div className="p-1">
                <input
                  type="text"
                  aria-label="New workspace name"
                  autoFocus
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      submit();
                    }
                  }}
                  placeholder="Workspace name"
                  className="border-border-subtle bg-surface-1 text-foreground placeholder:text-muted-foreground w-full rounded-md border px-2 py-1.5 text-[11px] outline-none"
                />
                <div className="mt-1.5 flex items-center justify-end gap-1.5">
                  <button
                    type="button"
                    onClick={() => {
                      setAdding(false);
                      setName("");
                    }}
                    className="text-muted-foreground hover:text-foreground rounded-md px-2 py-1 text-[11px] font-semibold"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={submit}
                    disabled={!name.trim() || creating}
                    className="border-border-subtle bg-surface-1 text-foreground hover:bg-surface-2 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {creating ? <Loader2 size={11} className="animate-spin" /> : null}
                    Add workspace
                  </button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  setShowDirectoryPicker(true);
                }}
                className="text-foreground/80 hover:bg-white/[0.06] flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs font-semibold transition-all"
              >
                <FolderPlus size={12} />
                Add workspace…
              </button>
            )}
          </div>
        </div>
      ) : null}

      <SelectWorkspaceDirectoryModal
        open={showDirectoryPicker}
        busy={creating}
        onClose={() => setShowDirectoryPicker(false)}
        onSelect={(path, selectedName) => {
          onCreateWorkspace(selectedName, path);
        }}
      />
    </div>
  );
}

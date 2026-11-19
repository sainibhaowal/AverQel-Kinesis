"use client";

import { useEffect, useRef, useState, useMemo, useCallback } from "react";
import { createPortal } from "react-dom";
import { Folder, File, ChevronRight } from "lucide-react";
import { fetchWithAuth } from "@/lib/api";

interface FileReferenceCandidate {
  path: string;
  is_directory: boolean;
  size: number | null;
  mime_type: string | null;
}

interface FileReferenceListResponse {
  query: string;
  candidates: FileReferenceCandidate[];
  count: number;
}

interface DeepSpaceFileMentionsProps {
  isOpen: boolean;
  onClose: () => void;
  onSelect: (path: string) => void;
  conversationId?: string | null;
  inputValue: string;
  cursorPosition: number;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
}

function formatFileSize(bytes: number | null): string {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getFileIcon(mimeType: string | null, isDirectory: boolean) {
  if (isDirectory) return <Folder className="h-4 w-4 text-amber-500" />;
  if (!mimeType) return <File className="h-4 w-4 text-slate-400" />;
  if (mimeType.startsWith("image/")) return <File className="h-4 w-4 text-emerald-500" />;
  if (mimeType.startsWith("text/") || mimeType === "application/json") return <File className="h-4 w-4 text-cyan-500" />;
  if (mimeType === "application/pdf") return <File className="h-4 w-4 text-red-500" />;
  return <File className="h-4 w-4 text-slate-400" />;
}

export default function DeepSpaceFileMentions({
  isOpen,
  onClose,
  onSelect,
  conversationId,
  inputValue,
  cursorPosition,
  textareaRef,
}: DeepSpaceFileMentionsProps) {
  const [candidates, setCandidates] = useState<FileReferenceCandidate[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 });
  const abortControllerRef = useRef<AbortController | null>(null);
  const hasFetchedRef = useRef(false);

  // Extract the @file mention prefix from current input
  const mentionPrefix = useMemo(() => {
    const textBeforeCursor = inputValue.slice(0, cursorPosition);
    const match = textBeforeCursor.match(/@([^\s@]*)$/);
    return match ? match[1] : "";
  }, [inputValue, cursorPosition]);

  // Update menu position when cursor position or input changes
  useEffect(() => {
    if (!textareaRef.current) return;
    const textarea = textareaRef.current;
    const rect = textarea.getBoundingClientRect();
    const textareaCoords = getTextareaCursorPosition(textarea, cursorPosition);
    setMenuPosition({
      top: rect.top + textareaCoords.top + 24,
      left: rect.left + textareaCoords.left,
    });
  }, [textareaRef, cursorPosition, inputValue]);

  // Fetch candidates when mention prefix changes
  useEffect(() => {
    if (!conversationId) {
      hasFetchedRef.current = false;
      return;
    }

    const fetchCandidates = async () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
      abortControllerRef.current = new AbortController();

      setLoading(true);
      try {
        const params = new URLSearchParams();
        if (mentionPrefix) params.set("query", mentionPrefix);
        params.set("conversation_id", conversationId);

        const response = await fetchWithAuth(
          `/host/file-references?${params.toString()}`,
          { signal: abortControllerRef.current.signal }
        );

        if (response.ok) {
          const data = (await response.json()) as FileReferenceListResponse;
          setCandidates(data.candidates);
          hasFetchedRef.current = true;
        }
      } catch (error) {
        if (error instanceof Error && error.name !== "AbortError") {
          console.warn("Failed to fetch file references:", error);
        }
      } finally {
        setLoading(false);
      }
    };

    // Only fetch when we have an @ trigger
    const textBeforeCursor = inputValue.slice(0, cursorPosition);
    if (textBeforeCursor.endsWith("@") || mentionPrefix) {
      fetchCandidates();
    } else if (hasFetchedRef.current) {
      // Only clear if we previously fetched results
      setCandidates([]);
      hasFetchedRef.current = false;
    }

    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [mentionPrefix, conversationId, inputValue, cursorPosition]);

  // Filter candidates based on search query (derived state, no separate state needed)
  const filteredCandidates = useMemo(() => {
    const query = searchQuery.toLowerCase();
    if (!query) return candidates;
    return candidates.filter(
      (candidate) =>
        candidate.path.toLowerCase().includes(query) ||
        (candidate.mime_type?.toLowerCase().includes(query) ?? false)
    );
  }, [searchQuery, candidates]);

  // Handle keyboard navigation
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (!isOpen) return;

    const maxIndex = filteredCandidates.length - 1;
    const clampIndex = (idx: number) => Math.max(0, Math.min(idx, maxIndex));

    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setSelectedIndex((prev) => clampIndex(prev + 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setSelectedIndex((prev) => clampIndex(prev - 1));
        break;
      case "Enter":
      case "Tab":
        e.preventDefault();
        const safeIndex = clampIndex(selectedIndex);
        if (filteredCandidates[safeIndex]) {
          const candidate = filteredCandidates[safeIndex];
          onSelect(candidate.path);
        }
        break;
      case "Escape":
        e.preventDefault();
        onClose();
        break;
    }
  }, [isOpen, filteredCandidates, selectedIndex, onSelect, onClose]);

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  // Focus search input when open
  useEffect(() => {
    const input = document.querySelector('input[aria-label="Filter file mentions"]') as HTMLInputElement | null;
    if (isOpen && input) {
      input.focus();
    }
  }, [isOpen]);

  if (!isOpen) return null;

  const menuContent = (
    <div
      className="deepspace-file-mentions fixed z-[1000] min-w-[280px] max-w-[480px] rounded-xl border border-slate-300/50 bg-white/95 shadow-xl backdrop-blur-md overflow-hidden dark:border-cyan-300/20 dark:bg-slate-900/95"
      style={{
        top: menuPosition.top,
        left: menuPosition.left,
      }}
      role="menu"
      aria-label="File mentions"
    >
      {/* Search input */}
      <div className="p-2 border-b border-slate-200/50 dark:border-slate-700/50">
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Filter files…"
          className="w-full rounded-lg border border-slate-300/50 bg-white/50 px-3 py-1.5 text-[12px] text-foreground placeholder:text-muted-foreground focus:border-cyan-400/50 focus:outline-none focus:ring-1 focus:ring-cyan-400/50 dark:border-slate-600/50 dark:bg-slate-800/50 dark:focus:border-cyan-400/50"
          aria-label="Filter file mentions"
        />
      </div>

      {/* Candidate list */}
      <div className="max-h-[300px] overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-6 text-muted-foreground">
            <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
            </svg>
            <span className="ml-2">Loading files…</span>
          </div>
        ) : filteredCandidates.length === 0 ? (
          <div className="px-3 py-4 text-center text-muted-foreground text-[12px]">
            {mentionPrefix
              ? `No files match "@${mentionPrefix}"`
              : "Type @path to search files"}
          </div>
        ) : (
          <ul className="py-1" role="listbox">
            {filteredCandidates.map((candidate, index) => (
              <li key={candidate.path} role="option" aria-selected={index === selectedIndex}>
                <button
                  type="button"
                  onClick={() => onSelect(candidate.path)}
                  onMouseEnter={() => setSelectedIndex(index)}
                  className={`w-full flex items-center gap-3 px-3 py-2 text-left text-[12px] transition-colors ${
                    index === selectedIndex
                      ? "bg-cyan-400/10 text-cyan-300 dark:bg-cyan-400/20 dark:text-cyan-200"
                      : "text-foreground/80 hover:bg-slate-100/50 dark:hover:bg-slate-800/50"
                  }`}
                >
                  {getFileIcon(candidate.mime_type, candidate.is_directory)}
                  <span className="flex-1 truncate font-mono text-[11px]">
                    {candidate.path}
                  </span>
                  {candidate.size !== null && (
                    <span className="font-mono text-[10px] text-muted-foreground shrink-0">
                      {formatFileSize(candidate.size)}
                    </span>
                  )}
                  {candidate.is_directory && (
                    <ChevronRight className="h-3 w-3 text-muted-foreground/50 shrink-0" aria-hidden="true" />
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Hint */}
      <div className="border-t border-slate-200/50 px-3 py-1.5 dark:border-slate-700/50">
        <p className="flex items-center gap-1.5 text-[10px] text-muted-foreground/70">
          <kbd className="px-1.5 py-0.5 rounded text-[9px] font-mono border border-slate-300/50 bg-slate-100/50 dark:border-slate-600/50 dark:bg-slate-800/50">
            ↑↓
          </kbd>
          <span>Navigate</span>
          <span className="mx-1">·</span>
          <kbd className="px-1.5 py-0.5 rounded text-[9px] font-mono border border-slate-300/50 bg-slate-100/50 dark:border-slate-600/50 dark:bg-slate-800/50">
            Enter
          </kbd>
          <span>Insert</span>
          <span className="mx-1">·</span>
          <kbd className="px-1.5 py-0.5 rounded text-[9px] font-mono border border-slate-300/50 bg-slate-100/50 dark:border-slate-600/50 dark:bg-slate-800/50">
            Esc
          </kbd>
          <span>Close</span>
        </p>
      </div>
    </div>
  );

  return createPortal(menuContent, document.body);
}

// Helper to get cursor position in textarea
function getTextareaCursorPosition(textarea: HTMLTextAreaElement, cursorPosition: number): { top: number; left: number } {
  const text = textarea.value.slice(0, cursorPosition);
  const lines = text.split("\n");
  const lineHeight = 22;
  const charWidth = 8.4;

  const lineIndex = lines.length - 1;
  const charIndex = lines[lineIndex].length;

  return {
    top: lineIndex * lineHeight + 8,
    left: charIndex * charWidth + 12,
  };
}

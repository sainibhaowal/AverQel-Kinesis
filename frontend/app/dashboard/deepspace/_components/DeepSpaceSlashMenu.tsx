"use client";

import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import { createPortal } from "react-dom";
import { ChevronRight, X, HelpCircle, GitBranch, ShieldCheck, Target, ListChecks, User, Settings, Download, MessageSquare } from "lucide-react";
import { fetchWithAuth } from "@/lib/api";

interface SlashCommand {
  name: string;
  description: string;
  usage?: string | null;
  aliases: string[];
}

interface DeepSpaceSlashMenuProps {
  isOpen: boolean;
  onClose: () => void;
  onExecute: (command: string, fullInput: string) => void | Promise<void>;
  conversationId?: string | null;
  inputValue: string;
  cursorPosition: number;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
}

const BUILTIN_COMMANDS: SlashCommand[] = [
  { name: "compact", description: "Trigger context compaction", usage: "/compact [--llm|--llm=true|-l]", aliases: [] },
  { name: "help", description: "Show available slash commands", usage: "/help [command]", aliases: ["?"] },
  { name: "permission", description: "Change permission mode", usage: "/permission <mode>", aliases: ["perm"] },
  { name: "goal", description: "Manage session goals", usage: "/goal <subcommand> [args...]", aliases: [] },
  { name: "plan", description: "Toggle plan mode", usage: "/plan [on|off]", aliases: [] },
  { name: "persona", description: "Manage agent persona", usage: "/persona <subcommand>", aliases: [] },
  { name: "skills", description: "Manage skills", usage: "/skills <subcommand>", aliases: [] },
  { name: "export", description: "Export conversation", usage: "/export [format]", aliases: [] },
  { name: "feedback", description: "Submit feedback", usage: "/feedback <positive|negative>", aliases: [] },
];

export default function DeepSpaceSlashMenu({
  isOpen,
  onClose,
  onExecute,
  conversationId,
  inputValue,
  cursorPosition,
  textareaRef,
}: DeepSpaceSlashMenuProps) {
  const [commands, setCommands] = useState<SlashCommand[]>(BUILTIN_COMMANDS);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [executing, setExecuting] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 });
  const menuRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // Extract the slash command prefix from current input
  const slashPrefix = useMemo(() => {
    const textBeforeCursor = inputValue.slice(0, cursorPosition);
    const match = textBeforeCursor.match(/\/(\S*)$/);
    return match ? match[1].toLowerCase() : "";
  }, [inputValue, cursorPosition]);

  // Filter commands based on search query (derived state, no useEffect needed)
  const filteredCommands = useMemo(() => {
    const query = searchQuery.toLowerCase();
    if (!query) {
      return commands;
    }
    return commands.filter(
      (cmd) =>
        cmd.name.toLowerCase().includes(query) ||
        cmd.description.toLowerCase().includes(query) ||
        cmd.aliases.some((alias) => alias.toLowerCase().includes(query))
    );
  }, [searchQuery, commands]);

  // Reset selection when filtered commands change
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSelectedIndex(0);
  }, [filteredCommands.length]);

  // Fetch dynamic commands from backend
  useEffect(() => {
    if (!conversationId) return;

    const fetchCommands = async () => {
      setLoading(true);
      try {
        const response = await fetchWithAuth(`/host/slash-commands`);
        if (response.ok) {
          const data = await response.json();
          const dynamicCommands: SlashCommand[] = data.map((cmd: Record<string, unknown>) => ({
            name: cmd.name,
            description: cmd.description,
            usage: cmd.usage,
            aliases: cmd.aliases || [],
          }));
          // Merge with built-in, avoiding duplicates
          const merged = [...BUILTIN_COMMANDS];
          for (const dyn of dynamicCommands) {
            if (!merged.some((b) => b.name === dyn.name)) {
              merged.push(dyn);
            }
          }
          setCommands(merged);
        }
      } catch (error) {
        console.warn("Failed to fetch slash commands:", error);
      } finally {
        setLoading(false);
      }
    };

    fetchCommands();
  }, [conversationId]);

  // Handle keyboard navigation
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (!isOpen) return;

    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setSelectedIndex((prev) => Math.min(prev + 1, filteredCommands.length - 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setSelectedIndex((prev) => Math.max(prev - 1, 0));
        break;
      case "Enter":
      case "Tab":
        e.preventDefault();
        if (filteredCommands[selectedIndex]) {
          const cmd = filteredCommands[selectedIndex];
          setExecuting(cmd.name);
          void onExecute(cmd.name, inputValue);
        }
        break;
      case "Escape":
        e.preventDefault();
        setExecuting(null);
        onClose();
        break;
    }
  }, [isOpen, filteredCommands, selectedIndex, onExecute, onClose, inputValue]);

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  // Focus search input when open
  useEffect(() => {
    if (isOpen && searchInputRef.current) {
      searchInputRef.current.focus();
    }
  }, [isOpen]);

  // Calculate menu position relative to textarea (in useEffect to avoid reading ref during render)
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

  if (!isOpen) return null;

  const menuContent = (
    <div
      ref={menuRef}
      className="deepspace-slash-menu fixed z-[1000] min-w-[280px] max-w-[400px] rounded-xl border border-slate-300/50 bg-white/95 shadow-xl backdrop-blur-md overflow-hidden dark:border-cyan-300/20 dark:bg-slate-900/95"
      style={{
        top: menuPosition.top,
        left: menuPosition.left,
      }}
      role="menu"
      aria-label="Slash commands"
    >
      {/* Search input */}
      <div className="p-2 border-b border-slate-200/50 dark:border-slate-700/50">
        <input
          ref={searchInputRef}
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search commands…"
          className="w-full rounded-lg border border-slate-300/50 bg-white/50 px-3 py-1.5 text-[12px] text-foreground placeholder:text-muted-foreground focus:border-cyan-400/50 focus:outline-none focus:ring-1 focus:ring-cyan-400/50 dark:border-slate-600/50 dark:bg-slate-800/50 dark:focus:border-cyan-400/50"
          aria-label="Filter slash commands"
        />
      </div>

      {/* Command list */}
      <div className="max-h-[300px] overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-6 text-muted-foreground">
            <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
            </svg>
            <span className="ml-2">Loading commands…</span>
          </div>
        ) : filteredCommands.length === 0 ? (
          <div className="px-3 py-4 text-center text-muted-foreground text-[12px]">
            {searchQuery
              ? `No commands match "${searchQuery}"`
              : "Type / to search commands"}
          </div>
        ) : (
          <ul className="py-1" role="listbox">
            {filteredCommands.map((cmd, index) => (
              <li key={cmd.name} role="option" aria-selected={index === selectedIndex}>
                <button
                  type="button"
                  onClick={() => {
                    setExecuting(cmd.name);
                    void onExecute(cmd.name, inputValue);
                  }}
                  onMouseEnter={() => setSelectedIndex(index)}
                  disabled={executing === cmd.name}
                  className={`w-full flex items-center gap-3 px-3 py-2 text-left text-[12px] transition-colors ${
                    index === selectedIndex
                      ? "bg-cyan-400/10 text-cyan-300 dark:bg-cyan-400/20 dark:text-cyan-200"
                      : "text-foreground/80 hover:bg-slate-100/50 dark:hover:bg-slate-800/50"
                  } ${
                    executing === cmd.name ? "opacity-50 cursor-wait" : ""
                  }`}
                >
                  {executing === cmd.name ? (
                    <svg className="animate-spin h-4 w-4 shrink-0" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                    </svg>
                  ) : (
                    <span className="font-mono font-semibold text-[11px]">
                      /{cmd.name}
                    </span>
                  )}
                  <span className="flex-1 truncate">{cmd.description}</span>
                  {cmd.usage && (
                    <span className="font-mono text-[10px] text-muted-foreground shrink-0">
                      {cmd.usage}
                    </span>
                  )}
                  {cmd.aliases.length > 0 && (
                    <span className="text-[9px] text-muted-foreground/60 shrink-0">
                      {cmd.aliases.map((a) => `/${a}`).join(", ")}
                    </span>
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
          <span>Execute</span>
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

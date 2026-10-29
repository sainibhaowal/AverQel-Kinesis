"use client";

import { createPortal } from "react-dom";
import { useEffect, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import {
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  FileText,
  Folder,
  FolderTree,
  Globe,
  Loader2,
  RefreshCw,
  SquareTerminal,
  X,
  GitBranch,
  Package,
  Copy,
  FileCode,
  Sparkles,
  Clock,
  AlertTriangle,
  CheckCircle,
} from "lucide-react";

import DeepSpaceJobsPanel from "./DeepSpaceJobsPanel";
import DeepSpaceTrajectoryView from "./DeepSpaceTrajectoryView";
import { fetchWithAuth } from "@/lib/api";
import { readConversationWorkspace } from "../_lib/workspace-api";
import type { Workspace } from "../_lib/workspace-api";
import {
  closeTerminal,
  listTerminals,
  listTerminalShells,
  listWorkspaceTree,
  openTerminal,
  readWorkspaceFile,
} from "../_lib/workspace-panel-api";
import type {
  TerminalSession,
  TerminalShell,
  TerminalShellOption,
  WorkspaceEntry,
  WorkspaceFile,
} from "../_lib/workspace-panel-api";
import DeepSpaceTerminal from "./DeepSpaceTerminal";
import {
  clickBrowser,
  goBrowserBack,
  goBrowserForward,
  navigateBrowser,
  openBrowserSession,
  pressBrowserKey,
  reloadBrowser,
  scrollBrowser,
  typeIntoBrowser,
} from "../_lib/browser-api";
import type { BrowserPageState } from "../_lib/browser-api";

/** The tools the side panel offers next to the DeepSpace chat. */
export type DeepSpaceSidePanelTab = "files" | "terminal" | "browser" | "jobs" | "workflows" | "trajectory" | "deliverables";

export interface DeepSpaceSidePanelProps {
  /** The conversation whose workspace, terminals, and browser preview are shown. */
  conversationId: string | null;
  /** The workspace in force for the conversation. */
  workspace?: Workspace | null;
  /** Whether the panel is visible. The panel renders nothing while it is closed. */
  open: boolean;
  /** Called when the user closes the panel. */
  onClose: () => void;
  /** The tab shown when the panel opens. Defaults to the files tab. */
  initialTab?: DeepSpaceSidePanelTab;
  /** The tab the panel shows. A change moves the panel to that tab. */
  tab?: DeepSpaceSidePanelTab;
  /** Called when the user picks another tab. */
  onTabChange?: (tab: DeepSpaceSidePanelTab) => void;
  /** The shell a new terminal session runs under when the tab opens. */
  terminalShell?: TerminalShell;
  /** Whether the terminal tab opens a session of its own when it appears. */
  startTerminal?: boolean;
}

const TAB_LIST: Array<{ id: DeepSpaceSidePanelTab; label: string; icon: typeof FolderTree }> = [
  { id: "files", label: "Files", icon: FolderTree },
  { id: "terminal", label: "Terminal", icon: SquareTerminal },
  { id: "browser", label: "Browser", icon: Globe },
];

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const MIN_PANEL_WIDTH = 22; // 22rem minimum
const MAX_PANEL_WIDTH = 64; // 64rem maximum
const DEFAULT_PANEL_WIDTH = 28; // 28rem default
const PANEL_WIDTH_STORAGE_KEY = "averqel_deepspace_side_panel_width";

/** The message the server sends when a conversation holds no browser session. */
const MISSING_SESSION_MESSAGE = "No such browser session.";

type WorkflowRun = {
  run_id: string | number;
  workflow: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
};

type TrajectoryTool = {
  tool_name: string;
  status: string;
  duration_ms: number | null;
};

type TrajectoryStep = {
  step: number;
  duration_ms: number | null;
  tools: TrajectoryTool[];
};

type TrajectoryTurn = {
  turn: number;
  duration_ms: number | null;
  steps: TrajectoryStep[];
};

type TrajectoryData = {
  turns: TrajectoryTurn[];
};

type ChangedFile = {
  path: string;
  write_count: number;
};

type DeclaredDeliverable = {
  path: string;
};

type DeliverablesData = {
  changed_files: ChangedFile[];
  declared_deliverables: DeclaredDeliverable[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

async function fetchPanelJson(endpoint: string, label: string): Promise<unknown> {
  const response = await fetchWithAuth(endpoint, { method: "GET" });
  if (typeof response.text !== "function") {
    if (!response.ok) throw new Error(`Failed to load ${label} (${response.status})`);
    return response.json();
  }
  const body = await response.text();
  if (!response.ok) {
    throw new Error(`Failed to load ${label} (${response.status})`);
  }
  if (!body.trim()) return null;
  try {
    return JSON.parse(body) as unknown;
  } catch {
    throw new Error(`${label} returned an invalid response. Restart the DeepSpace API and try again.`);
  }
}

function parseWorkflowRuns(payload: unknown): WorkflowRun[] {
  if (!Array.isArray(payload)) throw new Error("Workflow runs response was invalid");
  return payload.flatMap((item): WorkflowRun[] => {
    if (!isRecord(item)) return [];
    if (
      (typeof item.run_id !== "string" && typeof item.run_id !== "number") ||
      typeof item.workflow !== "string" ||
      typeof item.status !== "string"
    ) {
      return [];
    }
    return [{
      run_id: item.run_id,
      workflow: item.workflow,
      status: item.status,
      started_at: nullableString(item.started_at),
      completed_at: nullableString(item.completed_at),
    }];
  });
}

function parseTrajectory(payload: unknown): TrajectoryData {
  if (!isRecord(payload) || !Array.isArray(payload.turns)) {
    throw new Error("Trajectory response was invalid");
  }

  const turns = payload.turns.flatMap((turn): TrajectoryTurn[] => {
    if (!isRecord(turn) || typeof turn.turn !== "number") return [];
    const steps = Array.isArray(turn.steps)
      ? turn.steps.flatMap((step): TrajectoryStep[] => {
          if (!isRecord(step) || typeof step.step !== "number") return [];
          const tools = Array.isArray(step.tools)
            ? step.tools.flatMap((tool): TrajectoryTool[] => {
                if (
                  !isRecord(tool) ||
                  typeof tool.tool_name !== "string" ||
                  typeof tool.status !== "string"
                ) {
                  return [];
                }
                return [{
                  tool_name: tool.tool_name,
                  status: tool.status,
                  duration_ms: nullableNumber(tool.duration_ms),
                }];
              })
            : [];
          return [{
            step: step.step,
            duration_ms: nullableNumber(step.duration_ms),
            tools,
          }];
        })
      : [];
    return [{
      turn: turn.turn,
      duration_ms: nullableNumber(turn.duration_ms),
      steps,
    }];
  });
  return { turns };
}

function parseDeliverables(payload: unknown): DeliverablesData {
  if (!isRecord(payload)) throw new Error("Deliverables response was invalid");
  const changedFiles = Array.isArray(payload.changed_files)
    ? payload.changed_files.flatMap((file): ChangedFile[] => {
        if (!isRecord(file) || typeof file.path !== "string") return [];
        return [{
          path: file.path,
          write_count: typeof file.write_count === "number" ? file.write_count : 0,
        }];
      })
    : [];
  const declaredDeliverables = Array.isArray(payload.declared_deliverables)
    ? payload.declared_deliverables.flatMap((deliverable): DeclaredDeliverable[] => {
        if (!isRecord(deliverable) || typeof deliverable.path !== "string") return [];
        return [{ path: deliverable.path }];
      })
    : [];
  return { changed_files: changedFiles, declared_deliverables: declaredDeliverables };
}

/**
 * Read the message of a failed request for the panel's error states.
 */
function errorMessage(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback;
}

/**
 * Whether a failed request says that the conversation holds no session.
 *
 * The idle sweep closes a session nobody uses, so the panel meets this
 * answer when the user comes back to a tab that has been open a while.
 */
function isMissingSession(cause: unknown): boolean {
  return errorMessage(cause, "") === MISSING_SESSION_MESSAGE;
}

/**
 * The right-hand side panel of the DeepSpace chat.
 *
 * It shows the conversation workspace files, its terminal sessions, and an
 * in-app browser beside the chat. The panel is a fixed-width sibling column,
 * so the chat keeps its own width while the panel is open.
 */
export default function DeepSpaceSidePanel({
  conversationId,
  workspace,
  open,
  onClose,
  initialTab = "files",
  tab,
  onTabChange,
  terminalShell,
  startTerminal,
}: DeepSpaceSidePanelProps) {
  if (!open) return null;

  return (
    <SidePanel
      conversationId={conversationId}
      workspace={workspace}
      onClose={onClose}
      initialTab={initialTab}
      tab={tab}
      onTabChange={onTabChange}
      terminalShell={terminalShell}
      startTerminal={startTerminal}
    />
  );
}

interface SidePanelProps {
  /** The conversation whose workspace, terminals, and browser preview are shown. */
  conversationId: string | null;
  /** The workspace in force for the conversation. */
  workspace?: Workspace | null;
  /** Called when the user closes the panel. */
  onClose: () => void;
  /** The tab shown when the panel opens. */
  initialTab: DeepSpaceSidePanelTab;
  /** The tab the panel shows. A change moves the panel to that tab. */
  tab?: DeepSpaceSidePanelTab;
  /** Called when the user picks another tab. */
  onTabChange?: (tab: DeepSpaceSidePanelTab) => void;
  /** The shell a new terminal session runs under when the tab opens. */
  terminalShell?: TerminalShell;
  /** Whether the terminal tab opens a session of its own when it appears. */
  startTerminal?: boolean;
}

/** The open panel. It is mounted fresh every time the panel opens. */
function SidePanel({
  conversationId,
  workspace,
  onClose,
  initialTab,
  tab,
  onTabChange,
  terminalShell,
  startTerminal,
}: SidePanelProps) {
  const [ownTab, setOwnTab] = useState<DeepSpaceSidePanelTab>(initialTab);
  // The caller's tab wins when it names one, so the panel and the start
  // cards always show the same surface.
  const activeTab = tab ?? ownTab;

  const selectTab = (next: DeepSpaceSidePanelTab) => {
    setOwnTab(next);
    onTabChange?.(next);
  };

  // Resizable panel width state
  const [panelWidth, setPanelWidth] = useState(DEFAULT_PANEL_WIDTH);
  const [isResizing, setIsResizing] = useState(false);
  const resizeHandleRef = useRef<HTMLDivElement | null>(null);
  const resizeStartRef = useRef<{ mouseX: number; startWidth: number } | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const currentWidthRef = useRef<number>(panelWidth);

  useEffect(() => {
    currentWidthRef.current = panelWidth;
  }, [panelWidth]);

  // Load saved width on mount
  useEffect(() => {
    const savedWidth = localStorage.getItem(PANEL_WIDTH_STORAGE_KEY);
    if (savedWidth) {
      const parsed = parseFloat(savedWidth);
      if (!isNaN(parsed) && parsed >= MIN_PANEL_WIDTH && parsed <= MAX_PANEL_WIDTH) {
        queueMicrotask(() => {
          setPanelWidth(parsed);
          currentWidthRef.current = parsed;
        });
      }
    }
  }, []);

  // Save width on change
  useEffect(() => {
    localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(panelWidth));
  }, [panelWidth]);

  // Handle high-performance resize drag without React re-render bottleneck
  useEffect(() => {
    let animationFrameId: number | null = null;

    const handleMouseMove = (event: MouseEvent) => {
      if (!isResizing || !resizeStartRef.current) return;

      const mouseX = event.clientX;
      if (animationFrameId !== null) {
        cancelAnimationFrame(animationFrameId);
      }

      animationFrameId = requestAnimationFrame(() => {
        if (!resizeStartRef.current) return;
        const deltaX = resizeStartRef.current.mouseX - mouseX;
        const deltaRem = deltaX / 16; // Convert px delta to rem
        const rawWidth = resizeStartRef.current.startWidth + deltaRem;
        const maxAllowedRem = Math.min(MAX_PANEL_WIDTH, (window.innerWidth * 0.8) / 16);
        const clampedWidth = Math.min(Math.max(rawWidth, MIN_PANEL_WIDTH), Math.max(MIN_PANEL_WIDTH, maxAllowedRem));

        currentWidthRef.current = clampedWidth;
        if (panelRef.current) {
          panelRef.current.style.width = `${clampedWidth}rem`;
        }
      });
    };

    const handleMouseUp = () => {
      if (animationFrameId !== null) {
        cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
      }
      setIsResizing(false);
      resizeStartRef.current = null;
      setPanelWidth(currentWidthRef.current);
    };

    if (isResizing) {
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      document.addEventListener("mousemove", handleMouseMove, { passive: true });
      document.addEventListener("mouseup", handleMouseUp);
    }

    return () => {
      if (animationFrameId !== null) {
        cancelAnimationFrame(animationFrameId);
      }
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isResizing]);

  const handleResizeStart = (event: ReactMouseEvent) => {
    event.preventDefault();
    resizeStartRef.current = {
      mouseX: event.clientX,
      startWidth: currentWidthRef.current,
    };
    setIsResizing(true);
  };

  return (
    <aside
      ref={panelRef}
      aria-label="DeepSpace side panel"
      data-testid="deepspace-side-panel"
      className="deepspace-side-panel border-border-subtle bg-surface-2 relative flex h-full min-w-0 max-w-full shrink-0 flex-col overflow-hidden border-l"
      style={{
        width: `min(${panelWidth}rem, 100%)`,
        maxWidth: "100%",
        willChange: isResizing ? "width" : "auto",
      }}
    >
      {/* Resize handle slider on the left edge */}
      <div
        ref={resizeHandleRef}
        onMouseDown={handleResizeStart}
        className={`group absolute -left-1.5 top-0 bottom-0 w-3 cursor-col-resize z-30 flex items-center justify-center select-none transition-colors ${
          isResizing ? "bg-primary/20" : "hover:bg-primary/10"
        }`}
        aria-label="Resize panel"
        title="Drag to resize panel"
      >
        <div
          className={`h-full w-[2px] transition-colors ${
            isResizing ? "bg-primary shadow-[0_0_8px_rgba(var(--primary),0.8)]" : "bg-transparent group-hover:bg-primary/70"
          }`}
        />
      </div>

      <header className="border-border-subtle flex items-center gap-1.5 border-b px-2 py-2">
        <div
          role="tablist"
          aria-label="Conversation tools"
          className="scrollbar-hide flex min-w-0 flex-1 items-center gap-1 overflow-x-auto py-0.5"
        >
          {TAB_LIST.map((entry) => (
            <button
              key={entry.id}
              type="button"
              role="tab"
              id={`deepspace-side-panel-tab-${entry.id}`}
              aria-selected={activeTab === entry.id}
              aria-controls={`deepspace-side-panel-panel-${entry.id}`}
              onClick={() => selectTab(entry.id)}
              aria-label={entry.label}
              className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold whitespace-nowrap transition ${
                activeTab === entry.id
                  ? "border-border-subtle bg-surface-1 text-foreground shadow-xs"
                  : "text-muted-foreground hover:bg-surface-1 hover:text-foreground border-transparent"
              }`}
            >
              <entry.icon size={13} className="shrink-0" />
              <span>{entry.label}</span>
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close the side panel"
          title="Close panel"
          className="text-muted-foreground hover:bg-surface-1 hover:text-foreground shrink-0 rounded-md p-1.5 transition"
        >
          <X size={14} />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-hidden">
        {activeTab === "files" ? (
          <div
            role="tabpanel"
            id="deepspace-side-panel-panel-files"
            aria-labelledby="deepspace-side-panel-tab-files"
            tabIndex={0}
            className="focus-visible:ring-primary/40 flex h-full min-h-0 flex-col outline-none focus-visible:ring-1"
          >
            <FilesTab
              key={`${conversationId ?? "no-conversation"}:${workspace?.id ?? "default"}`}
              conversationId={conversationId}
              workspace={workspace}
            />
          </div>
        ) : null}
        {activeTab === "terminal" ? (
          <div
            role="tabpanel"
            id="deepspace-side-panel-panel-terminal"
            aria-labelledby="deepspace-side-panel-tab-terminal"
            tabIndex={0}
            className="focus-visible:ring-primary/40 flex h-full min-h-0 flex-col outline-none focus-visible:ring-1"
          >
            <TerminalTab
              key={conversationId ?? "no-conversation"}
              conversationId={conversationId}
              initialShell={terminalShell}
              startSession={startTerminal}
            />
          </div>
        ) : null}
        {activeTab === "browser" ? (
          <div
            role="tabpanel"
            id="deepspace-side-panel-panel-browser"
            aria-labelledby="deepspace-side-panel-tab-browser"
            tabIndex={0}
            className="focus-visible:ring-primary/40 flex h-full min-h-0 flex-col outline-none focus-visible:ring-1"
          >
            <BrowserTab key={conversationId ?? "no-conversation"} conversationId={conversationId} />
          </div>
        ) : null}
        {activeTab === "jobs" ? (
          <div
            role="tabpanel"
            id="deepspace-side-panel-panel-jobs"
            aria-labelledby="deepspace-side-panel-tab-jobs"
            tabIndex={0}
            className="focus-visible:ring-primary/40 flex h-full min-h-0 flex-col outline-none focus-visible:ring-1"
          >
            <DeepSpaceJobsPanel key={conversationId ?? "no-conversation"} conversationId={conversationId} />
          </div>
        ) : null}
        {activeTab === "workflows" ? (
          <div
            role="tabpanel"
            id="deepspace-side-panel-panel-workflows"
            aria-labelledby="deepspace-side-panel-tab-workflows"
            tabIndex={0}
            className="focus-visible:ring-primary/40 flex h-full min-h-0 flex-col outline-none focus-visible:ring-1"
          >
            <WorkflowsTab key={conversationId ?? "no-conversation"} conversationId={conversationId} />
          </div>
        ) : null}
        {activeTab === "trajectory" ? (
          <div
            role="tabpanel"
            id="deepspace-side-panel-panel-trajectory"
            aria-labelledby="deepspace-side-panel-tab-trajectory"
            tabIndex={0}
            className="focus-visible:ring-primary/40 flex h-full min-h-0 flex-col outline-none focus-visible:ring-1"
          >
            <TrajectoryTab key={conversationId ?? "no-conversation"} conversationId={conversationId} />
          </div>
        ) : null}
        {activeTab === "deliverables" ? (
          <div
            role="tabpanel"
            id="deepspace-side-panel-panel-deliverables"
            aria-labelledby="deepspace-side-panel-tab-deliverables"
            tabIndex={0}
            className="focus-visible:ring-primary/40 flex h-full min-h-0 flex-col outline-none focus-visible:ring-1"
          >
            <DeliverablesTab key={conversationId ?? "no-conversation"} conversationId={conversationId} />
          </div>
        ) : null}
      </div>
    </aside>
  );
}



/**
 * The workflow runs of one conversation.
 */
export function WorkflowsTab({ conversationId }: { conversationId: string | null }) {
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const [statusFilter, setStatusFilter] = useState<"all" | "completed" | "running" | "failed">("all");
  const [expandedId, setExpandedId] = useState<string | number | null>(null);
  const [copiedId, setCopiedId] = useState<string | number | null>(null);

  useEffect(() => {
    if (!conversationId) return;
    let cancelled = false;
    void (async () => {
      try {
        const data = await fetchPanelJson(
          `/deepspace/chats/${encodeURIComponent(conversationId)}/workflow-runs?limit=50`,
          "workflow runs",
        );
        if (cancelled) return;
        setRuns(parseWorkflowRuns(data));
      } catch (cause) {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : "Workflow runs could not be loaded.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [conversationId, refreshToken]);

  const copyToClipboard = async (text: string, id: string | number) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(id);
      setTimeout(() => setCopiedId(null), 2000);
    } catch {}
  };

  const filteredRuns = runs.filter((r) => {
    if (statusFilter === "all") return true;
    return r.status === statusFilter;
  });

  if (!conversationId) {
    return (
      <div className="flex h-full min-h-0 items-center justify-center p-4 text-[11px] text-muted-foreground">
        Open a conversation to see its workflow orchestration runs.
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface-0 font-sans text-foreground dark:bg-neutral-950">
      {/* Header bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-subtle bg-surface-1/70 px-3 py-2 text-xs backdrop-blur dark:bg-neutral-900/70">
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5 font-semibold text-[11px] text-foreground">
            <GitBranch size={13} className="text-primary" />
            <span>Workflow Runs</span>
          </div>
          <span className="text-[10px] text-muted-foreground">
            ({runs.length} runs)
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          <div className="flex items-center rounded-lg border border-border-subtle bg-surface-2/60 p-0.5 text-[10px]">
            {(["all", "completed", "running", "failed"] as const).map((s) => (
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
            onClick={() => {
              setLoading(true);
              setError(null);
              setRefreshToken((value) => value + 1);
            }}
            disabled={loading}
            aria-label="Refresh workflow runs"
            title="Refresh runs"
            className="rounded-md border border-border-subtle bg-surface-2/60 p-1 text-muted-foreground hover:bg-surface-2 hover:text-foreground transition"
          >
            <RefreshCw size={12} className={loading ? "animate-spin text-primary" : ""} />
          </button>
        </div>
      </div>

      {error && (
        <div className="m-3 rounded-lg border border-rose-300/15 bg-rose-500/10 p-2.5 text-xs text-rose-200">
          {error}
        </div>
      )}

      {/* Runs List */}
      <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto p-3">
        {loading && runs.length === 0 ? (
          <div className="flex h-48 items-center justify-center gap-2 text-xs text-muted-foreground">
            <Loader2 size={16} className="animate-spin text-primary" />
            <span>Loading workflow runs…</span>
          </div>
        ) : filteredRuns.length === 0 ? (
          <div className="flex h-48 flex-col items-center justify-center gap-1 text-center">
            <GitBranch size={24} className="text-muted-foreground/40" />
            <p className="text-xs font-semibold text-foreground">No Workflow Runs</p>
            <p className="text-[11px] text-muted-foreground">
              {statusFilter === "all"
                ? "Fan-out delegations and multi-step pipeline scripts will appear here."
                : `No runs currently matching status "${statusFilter}".`}
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {filteredRuns.map((run) => {
              const isExpanded = expandedId === run.run_id;
              const isRunning = run.status === "running";
              const isCompleted = run.status === "completed";
              const isFailed = run.status === "failed";

              let badgeColor = "border-amber-500/30 bg-amber-500/10 text-amber-400";
              if (isCompleted) badgeColor = "border-emerald-500/30 bg-emerald-500/10 text-emerald-400";
              else if (isFailed) badgeColor = "border-rose-500/30 bg-rose-500/10 text-rose-400";
              else if (isRunning) badgeColor = "border-cyan-500/30 bg-cyan-500/10 text-cyan-400";

              return (
                <div
                  key={run.run_id}
                  className="rounded-lg border border-border-subtle/80 bg-surface-1/40 hover:bg-surface-1/70 dark:bg-neutral-900/40 transition"
                >
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setExpandedId(isExpanded ? null : run.run_id)}
                    className="flex items-center justify-between gap-2.5 p-2.5 cursor-pointer text-left"
                  >
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      <div className="rounded-md bg-surface-2 p-1 text-muted-foreground">
                        <GitBranch size={13} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-[11px] font-semibold text-foreground truncate">
                            {run.workflow}
                          </span>
                          <span className="font-mono text-[10px] text-muted-foreground/70 truncate">
                            #{run.run_id}
                          </span>
                        </div>
                        <div className="text-[10px] text-muted-foreground mt-0.5 truncate">
                          {run.started_at ? `Started ${new Date(run.started_at).toLocaleTimeString()}` : "Pending"}
                          {run.completed_at && ` → ${new Date(run.completed_at).toLocaleTimeString()}`}
                        </div>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span
                        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-semibold capitalize ${badgeColor}`}
                      >
                        {isCompleted && <CheckCircle size={10} />}
                        {isFailed && <AlertTriangle size={10} />}
                        {isRunning && <Loader2 size={10} className="animate-spin" />}
                        <span>{run.status}</span>
                      </span>
                      <div className="text-muted-foreground/60">
                        {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </div>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="border-t border-border-subtle/70 bg-surface-0/60 p-3 text-xs dark:bg-neutral-950/60">
                      <div className="flex items-center justify-between pb-2 mb-2 border-b border-border-subtle/50 text-[10px] text-muted-foreground">
                        <span>Run ID: <strong className="font-mono text-foreground">{run.run_id}</strong></span>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            copyToClipboard(String(run.run_id), run.run_id);
                          }}
                          className="inline-flex items-center gap-1 rounded bg-surface-2 px-1.5 py-0.5 text-[10px] hover:text-foreground transition"
                        >
                          {copiedId === run.run_id ? (
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

                      <div className="grid grid-cols-2 gap-2 text-[10px] text-muted-foreground mb-1">
                        {run.started_at && (
                          <div>
                            <span className="block font-semibold uppercase text-[9px]">Started:</span>
                            <span className="text-foreground">{new Date(run.started_at).toLocaleString()}</span>
                          </div>
                        )}
                        {run.completed_at && (
                          <div>
                            <span className="block font-semibold uppercase text-[9px]">Completed:</span>
                            <span className="text-foreground">{new Date(run.completed_at).toLocaleString()}</span>
                          </div>
                        )}
                      </div>
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

/**
 * The trajectory (timing ledger) of one conversation.
 */
export function TrajectoryTab({ conversationId }: { conversationId: string | null }) {
  if (!conversationId) {
    return (
      <div className="flex h-full min-h-0 items-center justify-center px-3">
        <p className="text-muted-foreground text-[11px]">Open a conversation to see its trajectory.</p>
      </div>
    );
  }

  return <DeepSpaceTrajectoryView conversationId={conversationId} />;
}

/**
 * The deliverables (changed files & declared outputs) of one conversation.
 */
export function DeliverablesTab({ conversationId }: { conversationId: string | null }) {
  const [data, setData] = useState<DeliverablesData>({ changed_files: [], declared_deliverables: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [copiedPath, setCopiedPath] = useState<string | null>(null);

  useEffect(() => {
    if (!conversationId) return;
    let cancelled = false;
    void (async () => {
      try {
        const result = await fetchPanelJson(
          `/deepspace/chats/${encodeURIComponent(conversationId)}/deliverables?limit=100`,
          "deliverables",
        );
        if (cancelled) return;
        setData(parseDeliverables(result));
      } catch (cause) {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : "Deliverables could not be loaded.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [conversationId, refreshToken]);

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedPath(text);
      setTimeout(() => setCopiedPath(null), 2000);
    } catch {}
  };

  const query = searchQuery.trim().toLowerCase();
  const filteredChanged = (data.changed_files || []).filter((f) =>
    !query || f.path.toLowerCase().includes(query)
  );
  const filteredDeclared = (data.declared_deliverables || []).filter((d) =>
    !query || d.path.toLowerCase().includes(query)
  );

  const totalWrites = (data.changed_files || []).reduce((acc, f) => acc + (f.write_count || 1), 0);

  if (!conversationId) {
    return (
      <div className="flex h-full min-h-0 items-center justify-center p-4 text-[11px] text-muted-foreground">
        Open a conversation to see its deliverables.
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface-0 font-sans text-foreground dark:bg-neutral-950">
      {/* Header bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-subtle bg-surface-1/70 px-3 py-2 text-xs backdrop-blur dark:bg-neutral-900/70">
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5 font-semibold text-[11px] text-foreground">
            <Package size={13} className="text-primary" />
            <span>Deliverables</span>
          </div>
          <span className="text-[10px] text-muted-foreground">
            ({(data.declared_deliverables || []).length} declared · {(data.changed_files || []).length} modified · {totalWrites} writes)
          </span>
        </div>

        <div className="flex items-center gap-2">
          <input
            type="search"
            placeholder="Filter files…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="h-6 w-32 sm:w-44 rounded-md border border-border-subtle bg-surface-0 px-2 text-[10px] text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none dark:bg-neutral-900"
          />

          <button
            onClick={() => {
              setLoading(true);
              setError(null);
              setRefreshToken((value) => value + 1);
            }}
            disabled={loading}
            aria-label="Refresh deliverables"
            title="Refresh deliverables"
            className="rounded-md border border-border-subtle bg-surface-2/60 p-1 text-muted-foreground hover:bg-surface-2 hover:text-foreground transition"
          >
            <RefreshCw size={12} className={loading ? "animate-spin text-primary" : ""} />
          </button>
        </div>
      </div>

      {error && (
        <div className="m-3 rounded-lg border border-rose-300/15 bg-rose-500/10 p-2.5 text-xs text-rose-200">
          {error}
        </div>
      )}

      {/* Body List */}
      <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto p-3 space-y-4">
        {loading && (data.changed_files?.length === 0 && data.declared_deliverables?.length === 0) ? (
          <div className="flex h-48 items-center justify-center gap-2 text-xs text-muted-foreground">
            <Loader2 size={16} className="animate-spin text-primary" />
            <span>Inspecting delivered artifacts and modified files…</span>
          </div>
        ) : filteredChanged.length === 0 && filteredDeclared.length === 0 ? (
          <div className="flex h-48 flex-col items-center justify-center gap-1 text-center">
            <Package size={24} className="text-muted-foreground/40" />
            <p className="text-xs font-semibold text-foreground">No Deliverables Yet</p>
            <p className="text-[11px] text-muted-foreground">
              {query
                ? `No files matching filter "${query}".`
                : "Files created, modified, or presented during the conversation will appear here."}
            </p>
          </div>
        ) : (
          <>
            {/* 1. Declared Deliverables Section */}
            {filteredDeclared.length > 0 && (
              <div>
                <div className="flex items-center gap-1.5 mb-2 text-[10px] font-bold tracking-[0.16em] uppercase text-primary">
                  <Sparkles size={12} />
                  <span>Final Delivered Artifacts</span>
                </div>
                <div className="space-y-1.5">
                  {filteredDeclared.map((deliverable) => {
                    const ext = deliverable.path.split(".").pop() || "";
                    const fileName = deliverable.path.split("/").pop() || deliverable.path;

                    return (
                      <div
                        key={deliverable.path}
                        className="flex items-center justify-between gap-2.5 rounded-lg border border-primary/30 bg-primary/5 p-2.5 shadow-xs transition hover:bg-primary/10"
                      >
                        <div className="flex items-center gap-2 min-w-0 flex-1">
                          <div className="rounded-md bg-primary/15 p-1.5 text-primary">
                            <Sparkles size={14} />
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <span className="text-[12px] font-bold text-foreground truncate">
                                {fileName}
                              </span>
                              {ext && (
                                <span className="rounded bg-primary/20 px-1 py-0.2 font-mono text-[9px] uppercase text-primary font-semibold">
                                  .{ext}
                                </span>
                              )}
                            </div>
                            <p className="font-mono text-[10px] text-muted-foreground truncate">
                              {deliverable.path}
                            </p>
                          </div>
                        </div>

                        <button
                          type="button"
                          onClick={() => copyToClipboard(deliverable.path)}
                          title="Copy file path"
                          className="rounded-md border border-border-subtle bg-surface-1 p-1.5 text-muted-foreground hover:bg-surface-2 hover:text-foreground transition"
                        >
                          {copiedPath === deliverable.path ? (
                            <Check size={12} className="text-emerald-400" />
                          ) : (
                            <Copy size={12} />
                          )}
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* 2. Changed Files Section */}
            {filteredChanged.length > 0 && (
              <div>
                <div className="flex items-center gap-1.5 mb-2 text-[10px] font-bold tracking-[0.16em] uppercase text-muted-foreground">
                  <FileCode size={12} />
                  <span>Modified Files ({filteredChanged.length})</span>
                </div>
                <div className="space-y-1">
                  {filteredChanged.map((file) => {
                    const isCode = /\.(ts|tsx|js|jsx|py|json|yml|yaml|css|html|go|rs|cpp|c|sh)$/i.test(file.path);
                    const FileIcon = isCode ? FileCode : FileText;
                    const fileName = file.path.split("/").pop() || file.path;

                    return (
                      <div
                        key={file.path}
                        className="group flex items-center justify-between gap-2.5 rounded-lg border border-border-subtle/80 bg-surface-1/40 p-2 text-xs transition hover:bg-surface-1 hover:border-border-subtle"
                      >
                        <div className="flex items-center gap-2 min-w-0 flex-1">
                          <FileIcon size={13} className="text-muted-foreground shrink-0" />
                          <div className="min-w-0 flex-1">
                            <span className="font-mono text-[11px] text-foreground truncate block">
                              {file.path}
                            </span>
                          </div>
                        </div>

                        <div className="flex items-center gap-2 shrink-0">
                          <span className="rounded-full bg-surface-2 px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground border border-border-subtle/60">
                            {file.write_count} {file.write_count === 1 ? "write" : "writes"}
                          </span>
                          <button
                            type="button"
                            onClick={() => copyToClipboard(file.path)}
                            title="Copy file path"
                            className="rounded p-1 text-muted-foreground hover:bg-surface-2 hover:text-foreground transition"
                          >
                            {copiedPath === file.path ? (
                              <Check size={11} className="text-emerald-400" />
                            ) : (
                              <Copy size={11} />
                            )}
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}


interface FilesTabProps {
  /** The conversation whose workspace tree is shown. */
  conversationId: string | null;
  /** The workspace in force for the conversation. */
  workspace?: Workspace | null;
}

interface DirectoryListing {
  entries: WorkspaceEntry[];
  error: string | null;
  /** The refresh token this listing was fetched for; an older one is stale. */
  token: number;
}

/**
 * The workspace files of one conversation.
 *
 * Directories expand inline and fetch their entries on the first open. Clicking
 * a file loads its text into the preview below the tree.
 */
function FilesTab({ conversationId, workspace }: FilesTabProps) {
  const [directories, setDirectories] = useState<Record<string, DirectoryListing>>({});
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [file, setFile] = useState<WorkspaceFile | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const [activeWorkspace, setActiveWorkspace] = useState<Workspace | null>(workspace ?? null);

  useEffect(() => {
    if (workspace) {
      setActiveWorkspace(workspace);
      setDirectories({});
      setExpandedPaths(new Set());
      setFile(null);
      setFileError(null);
      setRefreshToken((prev) => prev + 1);
    }
  }, [workspace?.id, workspace?.path]);

  useEffect(() => {
    if (!conversationId) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const info = await readConversationWorkspace(conversationId);
        if (!cancelled && info.workspace) {
          setActiveWorkspace(info.workspace);
        }
      } catch {
        // Fall back gracefully
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, refreshToken]);

  const [diskRoot, setDiskRoot] = useState<string>("");

  useEffect(() => {
    if (!conversationId) return;
    const visiblePaths = ["", ...expandedPaths];
    const stalePaths = visiblePaths.filter((path) => directories[path]?.token !== refreshToken);
    if (stalePaths.length === 0) return;
    let cancelled = false;
    void (async () => {
      const loaded = await Promise.all(
        stalePaths.map(async (path) => {
          try {
            const result = await listWorkspaceTree(conversationId, path || undefined);
            if (!path && result.root) {
              setDiskRoot(result.root);
            }
            return { path, entries: result.entries, error: null };
          } catch (cause) {
            return {
              path,
              entries: [] as WorkspaceEntry[],
              error: errorMessage(cause, "The workspace files could not be loaded."),
            };
          }
        }),
      );
      if (cancelled) return;
      setDirectories((current) => {
        const next = { ...current };
        for (const result of loaded) {
          next[result.path] = {
            entries: result.entries,
            error: result.error,
            token: refreshToken,
          };
        }
        return next;
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, expandedPaths, directories, refreshToken]);

  const rootListing = directories[""];
  const rootEntries =
    rootListing && rootListing.token === refreshToken ? rootListing.entries : null;
  const error = rootListing && rootListing.token === refreshToken ? rootListing.error : null;

  const toggleDirectory = (entry: WorkspaceEntry) => {
    setExpandedPaths((current) => {
      const next = new Set(current);
      if (next.has(entry.path)) next.delete(entry.path);
      else next.add(entry.path);
      return next;
    });
  };

  const openFile = async (entry: WorkspaceEntry) => {
    if (!conversationId) return;
    setFileLoading(true);
    setFileError(null);
    try {
      const loaded = await readWorkspaceFile(conversationId, entry.path);
      setFile(loaded);
    } catch (cause) {
      setFile(null);
      setFileError(errorMessage(cause, "The file could not be read."));
    } finally {
      setFileLoading(false);
    }
  };

  const renderEntries = (listed: WorkspaceEntry[], depth: number) =>
    listed.map((entry) => {
      if (entry.kind === "directory") {
        const isExpanded = expandedPaths.has(entry.path);
        const children = directories[entry.path];
        const childrenReady = Boolean(children) && children?.token === refreshToken;
        return (
          <li key={entry.path}>
            <button
              type="button"
              aria-expanded={isExpanded}
              onClick={() => toggleDirectory(entry)}
              style={{ paddingLeft: `${depth * 12 + 6}px` }}
              className="text-foreground/80 hover:bg-surface-1 flex w-full items-center gap-1.5 rounded-md py-1.5 pr-2 text-left text-[11px] transition"
            >
              <ChevronRight
                size={12}
                className={`text-muted-foreground shrink-0 transition-transform ${isExpanded ? "rotate-90" : ""}`}
              />
              <Folder size={13} className="text-primary shrink-0" />
              <span className="truncate">{entry.name}</span>
            </button>
            {isExpanded ? (
              childrenReady ? (
                children && children.entries.length > 0 ? (
                  <ul className="space-y-0.5">{renderEntries(children.entries, depth + 1)}</ul>
                ) : children?.error ? (
                  <p role="alert" className="py-1.5 pl-6 text-[10px] leading-4 text-rose-200">
                    {children.error}
                  </p>
                ) : (
                  <p className="text-muted-foreground py-1.5 pl-6 text-[10px]">
                    Empty directory
                  </p>
                )
              ) : (
                <p className="text-muted-foreground flex items-center gap-1.5 py-1.5 pl-6 text-[10px]">
                  <Loader2 size={11} className="animate-spin" />
                  Loading folder
                </p>
              )
            ) : null}
          </li>
        );
      }
      return (
        <li key={entry.path}>
          <button
            type="button"
            onClick={() => void openFile(entry)}
            style={{ paddingLeft: `${depth * 12 + 20}px` }}
            className="text-foreground/70 hover:bg-surface-1 hover:text-foreground flex w-full items-center gap-1.5 rounded-md py-1.5 pr-2 text-left text-[11px] transition"
          >
            <FileText size={13} className="text-muted-foreground shrink-0" />
            <span className="truncate">{entry.name}</span>
          </button>
        </li>
      );
    });

  const displayPath = activeWorkspace?.path || diskRoot;
  const pathCut = displayPath ? Math.max(displayPath.lastIndexOf("/"), displayPath.lastIndexOf("\\")) + 1 : 0;
  const dirPart = displayPath ? displayPath.slice(0, pathCut) : "";
  const namePart = displayPath ? displayPath.slice(pathCut) : "";

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-border-subtle px-3 py-2">
        <span
          title={displayPath || undefined}
          className="min-w-0 flex-1 truncate text-[11px] font-mono select-text cursor-default"
        >
          {dirPart ? (
            <span className="text-muted-foreground opacity-60">
              {dirPart.replace(/^\/home\/[^/]+/, "~")}
            </span>
          ) : null}
          <span className="font-semibold text-foreground">{namePart || "Workspace"}</span>
        </span>
        <button
          type="button"
          aria-label="Refresh the workspace files"
          onClick={() => setRefreshToken((token) => token + 1)}
          className="text-muted-foreground hover:bg-surface-1 hover:text-foreground rounded-md p-1 transition shrink-0"
        >
          <RefreshCw size={12} className={rootEntries === null ? "animate-spin" : ""} />
        </button>
      </div>

      {error ? (
        <p
          role="alert"
          className="mx-3 mb-2 rounded-lg border border-rose-300/15 bg-rose-300/[0.06] px-2 py-1.5 text-[10px] leading-4 text-rose-200"
        >
          {error}
        </p>
      ) : null}

      <div className="custom-scrollbar min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {!conversationId ? (
          <p className="text-muted-foreground px-2 py-4 text-[11px]">
            Open a conversation to browse its workspace files.
          </p>
        ) : rootEntries === null ? (
          <div className="flex justify-center p-4">
            <Loader2 size={16} className="text-primary animate-spin" />
          </div>
        ) : rootEntries.length === 0 ? (
          <p className="text-muted-foreground px-2 py-4 text-[11px]">
            This conversation has no workspace files yet.
          </p>
        ) : (
          <ul className="space-y-0.5">{renderEntries(rootEntries, 0)}</ul>
        )}
      </div>

      {fileLoading || fileError || file ? (
        <div className="border-border-subtle flex h-1/2 min-h-0 flex-col border-t">
          <div className="flex items-center gap-2 px-3 py-1.5">
            <span className="min-w-0 flex-1 truncate text-[10px] font-semibold">
              {file?.path ?? ""}
            </span>
            {file?.truncated ? (
              <span className="text-muted-foreground text-[9px] tracking-[0.08em] uppercase">
                Truncated
              </span>
            ) : null}
            <button
              type="button"
              aria-label="Close the file preview"
              onClick={() => setFile(null)}
              className="text-muted-foreground hover:text-foreground rounded-md p-1 transition"
            >
              <X size={12} />
            </button>
          </div>
          {fileError ? (
            <p
              role="alert"
              className="mx-3 mb-2 rounded-lg border border-rose-300/15 bg-rose-300/[0.06] px-2 py-1.5 text-[10px] leading-4 text-rose-200"
            >
              {fileError}
            </p>
          ) : null}
          <pre className="custom-scrollbar min-h-0 flex-1 overflow-auto px-3 pb-3 font-mono text-[11px] leading-5 whitespace-pre-wrap">
            {file?.content ?? ""}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

interface TerminalTabProps {
  /** The conversation whose terminal sessions are shown. */
  conversationId: string | null;
  /** The shell a new session runs under when the tab opens. */
  initialShell?: TerminalShell;
  /** Whether the tab opens a session of its own when it appears. */
  startSession?: boolean;
}

interface TerminalListing {
  /** The request the listing belongs to; a newer request hides this listing. */
  key: string;
  sessions: TerminalSession[];
  error: string | null;
}

interface TerminalShellSelectProps {
  /** The shell selected for the next terminal session. */
  value: TerminalShell;
  /** The shells the host can start. */
  options: TerminalShellOption[];
  /** Whether opening the menu is currently unavailable. */
  disabled: boolean;
  /** Called after the user picks a shell. */
  onChange: (value: TerminalShell) => void;
}

/** A rounded shell picker whose menu does not use the browser's square native popup. */
function TerminalShellSelect({
  value,
  options,
  disabled,
  onChange,
}: TerminalShellSelectProps) {
  const [open, setOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState<{
    top: number;
    left: number;
    width: number;
  } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const selected = options.find((option) => option.id === value) ?? options[0];

  useEffect(() => {
    const close = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  useEffect(() => {
    if (!open) return;
    const updatePosition = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (rect) {
        setMenuPosition({
          top: rect.bottom + 4,
          left: rect.left,
          width: Math.max(rect.width, 96),
        });
      }
    };
    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open]);

  useEffect(() => {
    if (open) {
      const index = Math.max(
        0,
        options.findIndex((option) => option.id === (selected?.id ?? value)),
      );
      requestAnimationFrame(() => optionRefs.current[index]?.focus());
    }
  }, [open, options, selected?.id, value]);

  const choose = (next: TerminalShell) => {
    onChange(next);
    setOpen(false);
    triggerRef.current?.focus();
  };

  const focusOption = (index: number) => {
    const normalized = (index + options.length) % options.length;
    optionRefs.current[normalized]?.focus();
  };

  return (
    <div ref={rootRef} className="shrink-0">
      <button
        ref={triggerRef}
        type="button"
        aria-label="Terminal shell"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          } else if (event.key === "Escape") {
            setOpen(false);
          }
        }}
        className="border-border-subtle bg-surface-1 text-foreground hover:bg-surface-2 inline-flex min-w-[4.25rem] items-center justify-between gap-1 rounded-md border px-2 py-1 text-[11px] outline-none transition focus-visible:ring-1 focus-visible:ring-cyan-300/50 disabled:cursor-not-allowed disabled:opacity-40"
      >
        <span>{selected?.label ?? value}</span>
        <ChevronDown size={11} className={open ? "rotate-180 transition-transform" : "transition-transform"} />
      </button>
      {open && !disabled && menuPosition && typeof document !== "undefined"
        ? createPortal(
            <div
              ref={menuRef}
              role="listbox"
              aria-label="Terminal shell options"
              style={{
                position: "fixed",
                top: menuPosition.top,
                left: menuPosition.left,
                width: menuPosition.width,
              }}
              className="border-border-subtle bg-surface-1 z-[1000] overflow-hidden rounded-md border p-1 shadow-xl"
            >
              {options.map((option, index) => (
                <button
                  key={option.id}
                  ref={(element) => {
                    optionRefs.current[index] = element;
                  }}
                  type="button"
                  role="option"
                  aria-selected={option.id === value}
                  onClick={() => choose(option.id)}
                  onKeyDown={(event) => {
                    if (event.key === "ArrowDown") {
                      event.preventDefault();
                      focusOption(index + 1);
                    } else if (event.key === "ArrowUp") {
                      event.preventDefault();
                      focusOption(index - 1);
                    } else if (event.key === "Escape") {
                      event.preventDefault();
                      setOpen(false);
                      triggerRef.current?.focus();
                    }
                  }}
                  className={
                    option.id === value
                      ? "bg-primary text-primary-foreground block w-full rounded-sm px-2 py-1 text-left text-[11px] transition"
                      : "text-foreground hover:bg-surface-2 block w-full rounded-sm px-2 py-1 text-left text-[11px] transition"
                  }
                >
                  {option.label}
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}

/**
 * The terminal sessions of one conversation.
 *
 * The selected session is attached to a real xterm.js terminal over the
 * session's authenticated PTY stream.
 */
function TerminalTab({ conversationId, initialShell, startSession }: TerminalTabProps) {
  const [listing, setListing] = useState<TerminalListing | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [shell, setShell] = useState<TerminalShell>(initialShell ?? "bash");
  const [shellCapabilities, setShellCapabilities] = useState<{
    conversationId: string;
    shells: TerminalShellOption[];
    defaultShell: TerminalShell | null;
  } | null>(null);
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  // A tab that opens a session of its own waits for it before it reads the
  // conversation's sessions, so the list never races the new session.
  const [started, setStarted] = useState(!startSession);

  const requestKey = `${conversationId ?? ""}#${refreshToken}`;
  const currentShellCapabilities =
    shellCapabilities?.conversationId === conversationId ? shellCapabilities : null;
  const shellOptions = currentShellCapabilities?.shells ?? [];
  const shellsLoaded = currentShellCapabilities !== null;

  useEffect(() => {
    if (!conversationId) return;
    let cancelled = false;
    void (async () => {
      try {
        const capabilities = await listTerminalShells(conversationId);
        if (cancelled) return;
        setShellCapabilities({ conversationId, ...capabilities });
        setShell((current) => {
          if (initialShell && capabilities.shells.some((option) => option.id === initialShell)) {
            return initialShell;
          }
          return capabilities.defaultShell ?? capabilities.shells[0]?.id ?? current;
        });
      } catch (cause) {
        if (!cancelled) {
          setActionError(errorMessage(cause, "The available terminal shells could not be loaded."));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, initialShell]);

  useEffect(() => {
    if (!conversationId || !started) return;
    const key = `${conversationId}#${refreshToken}`;
    let cancelled = false;
    void (async () => {
      try {
        const listed = await listTerminals(conversationId);
        if (cancelled) return;
        setListing({ key, sessions: listed, error: null });
        setActiveSessionId((current) => current ?? listed[0]?.session_id ?? null);
      } catch (cause) {
        if (cancelled) return;
        setListing({
          key,
          sessions: [],
          error: errorMessage(cause, "The terminal sessions could not be loaded."),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, refreshToken, started]);

  // The tab opens a session of its own when the user asked for a terminal
  // from the start cards, so the card does what its name says.
  useEffect(() => {
    if (!startSession || !conversationId || !shellsLoaded) return;
    let cancelled = false;
    void (async () => {
      try {
        const opened = await openTerminal(conversationId, initialShell ?? shell);
        if (cancelled) return;
        setActiveSessionId(opened.session_id);
        setActionError(null);
      } catch (cause) {
        if (cancelled) return;
        setActionError(errorMessage(cause, "The terminal session could not be opened."));
      } finally {
        if (!cancelled) setStarted(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, initialShell, shell, shellsLoaded, startSession]);

  const currentListing = listing?.key === requestKey ? listing : null;
  const sessions = currentListing?.sessions ?? [];
  const activeSession = sessions.find((session) => session.session_id === activeSessionId);
  const loading = currentListing === null && Boolean(conversationId);
  const error = currentListing?.error ?? actionError;

  const selectSession = (sessionId: string) => {
    setActiveSessionId(sessionId);
    setActionError(null);
  };

  const createSession = async () => {
    if (!conversationId) return;
    setCreating(true);
    try {
      const opened = await openTerminal(conversationId, shell);
      setListing((current) => {
        const base =
          current && current.key === requestKey
            ? current
            : { key: requestKey, sessions: [], error: null };
        return {
          ...base,
          sessions: [
            ...base.sessions,
            {
              session_id: opened.session_id,
              shell: opened.shell,
              pty: opened.pty,
              cwd: opened.cwd,
              created_at: new Date().toISOString(),
              idle_seconds: 0,
            },
          ],
        };
      });
      setActiveSessionId(opened.session_id);
      setActionError(null);
    } catch (cause) {
      setActionError(errorMessage(cause, "The terminal session could not be opened."));
    } finally {
      setCreating(false);
    }
  };

  const closeSession = async (session: TerminalSession) => {
    if (!conversationId) return;
    try {
      await closeTerminal(conversationId, session.session_id);
      setListing((current) =>
        current
          ? {
              ...current,
              sessions: current.sessions.filter((item) => item.session_id !== session.session_id),
            }
          : current,
      );
      if (activeSessionId === session.session_id) {
        setActiveSessionId(null);
      }
      setActionError(null);
    } catch (cause) {
      setActionError(errorMessage(cause, "The terminal session could not be closed."));
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 px-3 py-2">
        <TerminalShellSelect
          value={shell}
          options={shellOptions}
          onChange={setShell}
          disabled={!shellOptions.length || creating}
        />
        <button
          type="button"
          onClick={() => void createSession()}
          disabled={creating || !conversationId || !shellsLoaded || !shellOptions.length}
          className="border-border-subtle bg-surface-1 text-foreground hover:bg-surface-2 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-40"
        >
          {creating ? <Loader2 size={12} className="animate-spin" /> : <SquareTerminal size={12} />}
          New terminal
        </button>
        <button
          type="button"
          aria-label="Refresh the terminal sessions"
          onClick={() => setRefreshToken((token) => token + 1)}
          className="text-muted-foreground hover:bg-surface-1 hover:text-foreground ml-auto rounded-md p-1 transition"
        >
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {error ? (
        <p
          role="alert"
          className="mx-3 mb-2 rounded-lg border border-rose-300/15 bg-rose-300/[0.06] px-2 py-1.5 text-[10px] leading-4 text-rose-200"
        >
          {error}
        </p>
      ) : null}

      <ul className="border-border-subtle max-h-32 overflow-y-auto border-b px-2 pb-2">
        {sessions.map((session) => (
          <li
            key={session.session_id}
            className={`flex items-center gap-2 rounded-md px-1.5 py-1 text-[11px] ${
              activeSessionId === session.session_id ? "bg-surface-1" : ""
            }`}
          >
            <button
              type="button"
              onClick={() => selectSession(session.session_id)}
              aria-current={activeSessionId === session.session_id}
              className="text-foreground/80 hover:text-foreground min-w-0 flex-1 truncate text-left"
            >
              {session.shell}
            </button>
            <span className="text-muted-foreground truncate text-[9px]">{session.session_id}</span>
            <button
              type="button"
              onClick={() => void closeSession(session)}
              aria-label={`Close the ${session.shell} session`}
              title={`Close the ${session.shell} session`}
              className="text-muted-foreground rounded-md px-1.5 py-0.5 text-[10px] transition hover:text-rose-200"
            >
              Close
            </button>
          </li>
        ))}
        {!loading && sessions.length === 0 ? (
          <li className="text-muted-foreground px-1.5 py-2 text-[11px]">
            No terminal sessions are running.
          </li>
        ) : null}
      </ul>

      {conversationId && activeSessionId ? (
        <DeepSpaceTerminal
          key={activeSessionId}
          conversationId={conversationId}
          sessionId={activeSessionId}
          initialCwd={activeSession?.cwd}
        />
      ) : (
        <div className="text-muted-foreground flex min-h-0 flex-1 items-center justify-center px-4 text-center text-[11px]">
          Select a terminal session or open a new one.
        </div>
      )}
    </div>
  );
}

/**
 * Whether a host may be framed without the user's permission.
 *
 * Loopback hosts and the app's own origin are allowed; every other host needs
 * the explicit "Allow external sites" confirmation.
 */
function isDefaultAllowedHost(url: URL, appOrigin: string): boolean {
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (LOOPBACK_HOSTS.has(host)) return true;
  return Boolean(appOrigin) && url.origin === appOrigin;
}

/** Whether the deployment runs a browser runtime the panel can drive. */
type BrowserRuntimeStatus = "checking" | "ready" | "unavailable";

/** What the panel knows about the deployment's browser runtime. */
interface BrowserRuntimeState {
  /** Whether the runtime is ready, still being checked, or absent. */
  status: BrowserRuntimeStatus;
  /** Why the runtime is absent, when it is absent. */
  reason: string | null;
  /** The page the browser showed when the tab opened. */
  page: BrowserPageState | null;
}

interface BrowserTabProps {
  /** The conversation whose browser session is driven. */
  conversationId: string | null;
}

/**
 * The in-app browser.
 *
 * A deployment with a browser runtime drives a real browser: the tab opens a
 * page, shows the browser's own screenshot of it, and the navigation buttons
 * move through the browser's own history.  A deployment without one keeps the
 * embedded view and says why, because a page the runtime cannot open is
 * still worth showing.
 */
function BrowserTab({ conversationId }: BrowserTabProps) {
  const [runtime, setRuntime] = useState<BrowserRuntimeState>({
    status: "checking",
    reason: null,
    page: null,
  });

  useEffect(() => {
    if (!conversationId) return;
    let cancelled = false;
    void (async () => {
      try {
        const opened = await openBrowserSession(conversationId);
        if (cancelled) return;
        setRuntime({ status: "ready", reason: null, page: opened });
      } catch (cause) {
        if (cancelled) return;
        setRuntime({
          status: "unavailable",
          reason: errorMessage(cause, "This deployment has no browser runtime."),
          page: null,
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  if (!conversationId) {
    return <EmbeddedBrowser notice="Open a conversation to browse with it." />;
  }
  if (runtime.status === "ready") {
    return <RuntimeBrowser conversationId={conversationId} initialPage={runtime.page} />;
  }
  return <EmbeddedBrowser notice={runtime.status === "unavailable" ? runtime.reason : null} />;
}

interface RuntimeBrowserProps {
  /** The conversation whose browser session is driven. */
  conversationId: string;
  /** The page the browser showed when the tab opened. */
  initialPage: BrowserPageState | null;
}

/** The distance one scroll button moves the page, in pixels. */
const BROWSER_SCROLL_STEP = 600;

/** The keys the browser tab offers as buttons. */
const BROWSER_KEY_BUTTONS: Array<{ key: string; label: string }> = [
  { key: "Enter", label: "Enter" },
  { key: "Tab", label: "Tab" },
  { key: "Backspace", label: "Backspace" },
  { key: "Escape", label: "Esc" },
];

/**
 * Map a click on the rendered screenshot to a point in the page viewport.
 *
 * The image is scaled to fit the panel and keeps its aspect ratio, so the
 * document area inside the element is narrower or shorter than the element.
 * The point is measured against that area and then divided by its scale.
 */
function pagePointFromClick(
  event: ReactMouseEvent<HTMLButtonElement>,
  image: HTMLImageElement | null,
  page: BrowserPageState,
): { x: number; y: number } | null {
  const rect = image?.getBoundingClientRect();
  if (!rect || rect.width <= 0 || rect.height <= 0) return null;
  if (event.detail === 0) {
    return { x: page.viewport_width / 2, y: page.viewport_height / 2 };
  }
  const scale = Math.min(rect.width / page.viewport_width, rect.height / page.viewport_height);
  if (!Number.isFinite(scale) || scale <= 0) return null;
  const left = rect.left + (rect.width - page.viewport_width * scale) / 2;
  const top = rect.top + (rect.height - page.viewport_height * scale) / 2;
  const x = (event.clientX - left) / scale;
  const y = (event.clientY - top) / scale;
  if (x < 0 || y < 0 || x >= page.viewport_width || y >= page.viewport_height) return null;
  return { x, y };
}

function BrowserEmptyState({
  onSelectUrl,
  notice,
}: {
  onSelectUrl?: (url: string) => void;
  notice?: string | null;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center p-6 text-center select-none bg-[#0a0d0c]">
      <div className="mb-3.5 flex h-14 w-14 items-center justify-center rounded-2xl border border-white/10 bg-gradient-to-b from-white/10 to-white/5 shadow-inner">
        <Globe size={26} className="text-emerald-400" />
      </div>
      <h4 className="text-sm font-semibold tracking-tight text-white/95">In-App Browser</h4>
      <p className="mt-1 max-w-[19rem] text-xs leading-5 text-white/60">
        Enter an HTTP(S) address to start browsing
      </p>
      {notice && (
        <p
          role="status"
          className="mt-2 max-w-[20rem] rounded-md border border-amber-500/20 bg-amber-500/10 px-2.5 py-1 text-[11px] text-amber-200"
        >
          {notice}
        </p>
      )}
      {onSelectUrl && (
        <div className="mt-5 w-full max-w-[22rem]">
          <span className="text-[10px] font-medium uppercase tracking-wider text-white/40">
            Quick connect
          </span>
          <div className="mt-2 flex flex-wrap justify-center gap-1.5">
            {["http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:8000"].map((sample) => (
              <button
                key={sample}
                type="button"
                onClick={() => onSelectUrl(sample)}
                className="rounded-lg border border-white/10 bg-white/5 px-2.5 py-1 text-[11px] font-mono text-white/75 transition hover:border-emerald-500/40 hover:bg-white/10 hover:text-white active:scale-95"
              >
                {sample.replace("http://", "")}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * The panel's view of the deployment's browser.
 *
 * The buttons drive the browser's own history, so going back returns to the
 * page the browser recorded and not to the address the user typed last.
 */
function RuntimeBrowser({ conversationId, initialPage }: RuntimeBrowserProps) {
  const [urlInput, setUrlInput] = useState("");
  const [allowExternal, setAllowExternal] = useState(true);
  const [page, setPage] = useState<BrowserPageState | null>(initialPage);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [textInput, setTextInput] = useState("");
  const screenshotRef = useRef<HTMLImageElement | null>(null);
  const appOrigin = typeof window === "undefined" ? "" : window.location.origin;

  const drive = async (action: () => Promise<BrowserPageState>, failure: string) => {
    setBusy(true);
    try {
      let result: BrowserPageState;
      try {
        result = await action();
      } catch (cause) {
        if (!isMissingSession(cause)) throw cause;
        // The idle sweep closed the session, so a new one is opened and the
        // command is sent again.
        await openBrowserSession(conversationId);
        result = await action();
      }
      setPage(result);
      setError(null);
    } catch (cause) {
      setError(errorMessage(cause, failure));
    } finally {
      setBusy(false);
    }
  };

  const open = () => {
    const raw = urlInput.trim();
    if (!raw) {
      setError("Enter a URL to open.");
      return;
    }
    let normalized = raw;
    const hasScheme =
      /^(https?|javascript|about|data|file|blob|mailto):/i.test(normalized) ||
      /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(normalized);
    if (!hasScheme) {
      if (
        normalized.startsWith("localhost") ||
        normalized.startsWith("127.0.0.1") ||
        normalized.startsWith("0.0.0.0") ||
        normalized.includes(":")
      ) {
        normalized = `http://${normalized}`;
      } else {
        normalized = `https://${normalized}`;
      }
    }
    let parsed: URL;
    try {
      parsed = new URL(normalized);
    } catch {
      setError("Enter a full URL, for example http://localhost:3000.");
      return;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      setError("Only http:// and https:// URLs can be opened.");
      return;
    }
    if (!allowExternal && !isDefaultAllowedHost(parsed, appOrigin)) {
      setError(`${parsed.host} is outside this app. Allow external sites to open it.`);
      return;
    }
    setError(null);
    void drive(
      () => navigateBrowser(conversationId, parsed.toString()),
      "That page could not be opened.",
    );
  };

  const clickPage = (event: ReactMouseEvent<HTMLButtonElement>) => {
    if (!page || busy) return;
    const point = pagePointFromClick(event, screenshotRef.current, page);
    if (!point) return;
    void drive(
      () => clickBrowser(conversationId, point.x, point.y),
      "The click could not be sent to the page.",
    );
  };

  const typeText = () => {
    if (!textInput) {
      setError("Enter text to type into the page.");
      return;
    }
    setError(null);
    void drive(
      () => typeIntoBrowser(conversationId, textInput),
      "The text could not be typed into the page.",
    );
  };

  const loadedUrl = page?.url ?? null;
  const screenshot = page?.screenshot_base64 ?? "";
  const isBlank = !loadedUrl || loadedUrl === "about:blank";

  return (
    <div className="flex h-full min-h-0 flex-col bg-[#0b0f0c]">
      {/* Top Address & Navigation Bar */}
      <div className="border-b border-white/10 bg-[#0e1411] p-2 space-y-2">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            open();
          }}
          className="flex items-center gap-1.5"
        >
          <div className="flex items-center gap-0.5">
            <button
              type="button"
              aria-label="Go back"
              title="Go back"
              disabled={!page?.can_go_back || busy}
              onClick={() =>
                void drive(
                  () => goBrowserBack(conversationId),
                  "The browser could not go back to the earlier page.",
                )
              }
              className="rounded-md p-1.5 text-white/70 hover:bg-white/10 hover:text-white transition disabled:opacity-20 disabled:cursor-not-allowed"
            >
              <ChevronLeft size={14} />
            </button>
            <button
              type="button"
              aria-label="Go forward"
              title="Go forward"
              disabled={!page?.can_go_forward || busy}
              onClick={() =>
                void drive(
                  () => goBrowserForward(conversationId),
                  "The browser could not go forward to the later page.",
                )
              }
              className="rounded-md p-1.5 text-white/70 hover:bg-white/10 hover:text-white transition disabled:opacity-20 disabled:cursor-not-allowed"
            >
              <ChevronRight size={14} />
            </button>
            <button
              type="button"
              aria-label="Reload the page"
              title="Reload the page"
              disabled={!loadedUrl || isBlank || busy}
              onClick={() =>
                void drive(() => reloadBrowser(conversationId), "The page could not be loaded again.")
              }
              className="rounded-md p-1.5 text-white/70 hover:bg-white/10 hover:text-white transition disabled:opacity-20 disabled:cursor-not-allowed"
            >
              <RefreshCw size={13} className={busy ? "animate-spin text-emerald-400" : ""} />
            </button>
          </div>

          <div className="relative min-w-0 flex-1">
            <input
              aria-label="Browser URL"
              value={urlInput}
              onChange={(event) => setUrlInput(event.target.value)}
              placeholder="Enter an HTTP(S) address"
              className="w-full rounded-lg border border-white/15 bg-black/40 px-3 py-1.5 text-xs text-white placeholder:text-white/35 outline-none transition focus:border-emerald-500/60 focus:ring-1 focus:ring-emerald-500/30"
            />
          </div>

          <button
            type="submit"
            disabled={busy || !urlInput.trim()}
            className="rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-black transition hover:bg-emerald-400 active:scale-95 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            {busy ? <Loader2 size={12} className="animate-spin" /> : "Go"}
          </button>
        </form>

        <div className="flex items-center justify-between px-1 text-[11px]">
          <label className="flex items-center gap-1.5 text-white/75 hover:text-white cursor-pointer select-none">
            <input
              type="checkbox"
              checked={allowExternal}
              onChange={(event) => setAllowExternal(event.target.checked)}
              className="sr-only"
            />
            <span
              className={`flex h-3.5 w-3.5 items-center justify-center rounded border transition ${
                allowExternal ? "border-emerald-400 bg-emerald-500 text-black" : "border-white/30 bg-white/5"
              }`}
            >
              {allowExternal && <Check size={10} strokeWidth={3} />}
            </span>
            <span>Allow external sites</span>
          </label>
        </div>
      </div>

      {/* Devtools / Interaction Toolbar */}
      <div className="border-b border-white/10 bg-[#0c100e] px-2.5 py-1.5 space-y-1.5">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            typeText();
          }}
          className="flex items-center gap-1.5"
        >
          <input
            aria-label="Text to type into the page"
            value={textInput}
            onChange={(event) => setTextInput(event.target.value)}
            placeholder="Type into the page"
            disabled={busy || isBlank}
            className="min-w-0 flex-1 rounded-md border border-white/10 bg-white/5 px-2.5 py-1 text-[11px] text-white placeholder:text-white/30 outline-none transition focus:border-white/30 disabled:opacity-30"
          />
          <button
            type="submit"
            disabled={busy || !textInput || isBlank}
            className="rounded-md border border-white/10 bg-white/10 px-2.5 py-1 text-[11px] font-semibold text-white/90 transition hover:bg-white/15 hover:text-white active:scale-95 disabled:cursor-not-allowed disabled:opacity-30"
          >
            Type
          </button>
        </form>

        <div className="flex flex-wrap items-center gap-1">
          <span className="text-[10px] font-medium text-white/40 mr-0.5">Keys</span>
          {BROWSER_KEY_BUTTONS.map((entry) => (
            <button
              key={entry.key}
              type="button"
              aria-label={`Press ${entry.label}`}
              title={`Press ${entry.label}`}
              disabled={busy || isBlank}
              onClick={() =>
                void drive(
                  () => pressBrowserKey(conversationId, entry.key),
                  `The ${entry.label} key could not be sent to the page.`,
                )
              }
              className="rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[10px] font-mono text-white/70 transition hover:border-white/20 hover:bg-white/10 hover:text-white active:scale-95 disabled:cursor-not-allowed disabled:opacity-30"
            >
              {entry.label}
            </button>
          ))}
          <div className="ml-auto flex items-center gap-0.5">
            <button
              type="button"
              aria-label="Scroll up"
              title="Scroll up"
              disabled={busy || isBlank}
              onClick={() =>
                void drive(
                  () => scrollBrowser(conversationId, 0, -BROWSER_SCROLL_STEP),
                  "The page could not be scrolled up.",
                )
              }
              className="rounded p-1 text-white/60 hover:bg-white/10 hover:text-white transition disabled:cursor-not-allowed disabled:opacity-20"
            >
              <ArrowUp size={12} />
            </button>
            <button
              type="button"
              aria-label="Scroll down"
              title="Scroll down"
              disabled={busy || isBlank}
              onClick={() =>
                void drive(
                  () => scrollBrowser(conversationId, 0, BROWSER_SCROLL_STEP),
                  "The page could not be scrolled down.",
                )
              }
              className="rounded p-1 text-white/60 hover:bg-white/10 hover:text-white transition disabled:cursor-not-allowed disabled:opacity-20"
            >
              <ArrowDown size={12} />
            </button>
          </div>
        </div>
      </div>

      {error ? (
        <p
          role="alert"
          className="mx-3 my-2 rounded-lg border border-rose-400/20 bg-rose-500/10 px-2.5 py-1.5 text-xs leading-4 text-rose-300"
        >
          {error}
        </p>
      ) : null}

      {/* Address Sub-bar */}
      <div className="flex items-center gap-2 border-b border-white/10 bg-[#0e1311] px-3 py-1.5 text-[10px]">
        <div className="flex items-center gap-1.5 min-w-0 flex-1">
          <span
            className={`h-1.5 w-1.5 rounded-full shrink-0 ${
              !isBlank ? "bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.6)]" : "bg-white/30"
            }`}
          />
          <span className="truncate font-mono text-white/60" title={loadedUrl ?? "about:blank"}>
            {loadedUrl && loadedUrl !== "about:blank" ? loadedUrl : "about:blank"}
          </span>
        </div>
        {loadedUrl && loadedUrl !== "about:blank" ? (
          <a
            href={loadedUrl}
            target="_blank"
            rel="noreferrer noopener"
            className="flex items-center gap-1 shrink-0 font-medium text-emerald-400 hover:text-emerald-300 transition"
          >
            <span>Open in new tab</span>
            <ExternalLink size={10} />
          </a>
        ) : null}
      </div>

      {/* Main Content Area */}
      <div className="min-h-0 flex-1 overflow-hidden bg-[#0a0d0c]">
        {!isBlank && screenshot ? (
          <button
            type="button"
            aria-label="Click the page"
            title="Click the page"
            disabled={busy}
            onClick={clickPage}
            className="relative block h-full w-full cursor-crosshair border-0 bg-[#0a0d0c] p-0 disabled:cursor-wait overflow-y-auto"
          >
            <img
              ref={screenshotRef}
              src={`data:image/png;base64,${screenshot}`}
              alt={page?.title ? `The page ${page.title}` : "The page the browser is showing"}
              draggable={false}
              className="pointer-events-none mx-auto block max-w-full object-contain object-top"
            />
            {busy && (
              <div className="absolute inset-0 flex items-center justify-center bg-black/40 backdrop-blur-[1px]">
                <Loader2 size={24} className="animate-spin text-emerald-400" />
              </div>
            )}
          </button>
        ) : (
          <BrowserEmptyState
            onSelectUrl={(sample) => {
              setUrlInput(sample);
              void drive(
                () => navigateBrowser(conversationId, sample),
                "That page could not be opened.",
              );
            }}
          />
        )}
      </div>
    </div>
  );
}

interface EmbeddedBrowserProps {
  /** Why the deployment has no browser runtime, when it has none. */
  notice: string | null;
}

/**
 * The embedded page view.
 *
 * Only http and https URLs are framed, and the frame runs without
 * `allow-same-origin` so the framed page cannot reach this app's origin.
 * The panel keeps the pages it visited, so the user can go back, go
 * forward, and reload the page that is open.
 */
function EmbeddedBrowser({ notice }: EmbeddedBrowserProps) {
  const [urlInput, setUrlInput] = useState("");
  const [history, setHistory] = useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const [allowExternal, setAllowExternal] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const appOrigin = typeof window === "undefined" ? "" : window.location.origin;

  const loadedUrl = historyIndex >= 0 ? (history[historyIndex] ?? null) : null;
  const canGoBack = historyIndex > 0;
  const canGoForward = historyIndex >= 0 && historyIndex < history.length - 1;

  const open = () => {
    const raw = urlInput.trim();
    if (!raw) {
      setError("Enter a URL to open.");
      return;
    }
    let normalized = raw;
    const hasScheme =
      /^(https?|javascript|about|data|file|blob|mailto):/i.test(normalized) ||
      /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(normalized);
    if (!hasScheme) {
      if (
        normalized.startsWith("localhost") ||
        normalized.startsWith("127.0.0.1") ||
        normalized.startsWith("0.0.0.0") ||
        normalized.includes(":")
      ) {
        normalized = `http://${normalized}`;
      } else {
        normalized = `https://${normalized}`;
      }
    }
    let parsed: URL;
    try {
      parsed = new URL(normalized);
    } catch {
      setError("Enter a full URL, for example http://localhost:3000.");
      return;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      setError("Only http:// and https:// URLs can be opened.");
      return;
    }
    if (!allowExternal && !isDefaultAllowedHost(parsed, appOrigin)) {
      setError(`${parsed.host} is outside this app. Allow external sites to open it.`);
      return;
    }
    setError(null);
    const next = parsed.toString();
    setHistory((current) => [...current.slice(0, historyIndex + 1), next]);
    setHistoryIndex((current) => current + 1);
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-[#0b0f0c]">
      {/* Top Address & Navigation Bar */}
      <div className="border-b border-white/10 bg-[#0e1411] p-2 space-y-2">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            open();
          }}
          className="flex items-center gap-1.5"
        >
          <div className="flex items-center gap-0.5">
            <button
              type="button"
              aria-label="Go back"
              title="Go back"
              disabled={!canGoBack}
              onClick={() => setHistoryIndex((current) => Math.max(0, current - 1))}
              className="rounded-md p-1.5 text-white/70 hover:bg-white/10 hover:text-white transition disabled:opacity-20 disabled:cursor-not-allowed"
            >
              <ChevronLeft size={14} />
            </button>
            <button
              type="button"
              aria-label="Go forward"
              title="Go forward"
              disabled={!canGoForward}
              onClick={() => setHistoryIndex((current) => Math.min(history.length - 1, current + 1))}
              className="rounded-md p-1.5 text-white/70 hover:bg-white/10 hover:text-white transition disabled:opacity-20 disabled:cursor-not-allowed"
            >
              <ChevronRight size={14} />
            </button>
            <button
              type="button"
              aria-label="Reload the page"
              title="Reload the page"
              disabled={!loadedUrl}
              onClick={() => setReloadToken((current) => current + 1)}
              className="rounded-md p-1.5 text-white/70 hover:bg-white/10 hover:text-white transition disabled:opacity-20 disabled:cursor-not-allowed"
            >
              <RefreshCw size={13} />
            </button>
          </div>

          <div className="relative min-w-0 flex-1">
            <input
              aria-label="Browser URL"
              value={urlInput}
              onChange={(event) => {
                setUrlInput(event.target.value);
                setAllowExternal(false);
              }}
              placeholder="Enter an HTTP(S) address"
              className="w-full rounded-lg border border-white/15 bg-black/40 px-3 py-1.5 text-xs text-white placeholder:text-white/35 outline-none transition focus:border-emerald-500/60 focus:ring-1 focus:ring-emerald-500/30"
            />
          </div>

          <button
            type="submit"
            disabled={!urlInput.trim()}
            className="rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-black transition hover:bg-emerald-400 active:scale-95 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            Go
          </button>
        </form>

        <div className="flex items-center justify-between px-1 text-[11px]">
          <label className="flex items-center gap-1.5 text-white/75 hover:text-white cursor-pointer select-none">
            <input
              type="checkbox"
              checked={allowExternal}
              onChange={(event) => setAllowExternal(event.target.checked)}
              className="sr-only"
            />
            <span
              className={`flex h-3.5 w-3.5 items-center justify-center rounded border transition ${
                allowExternal ? "border-emerald-400 bg-emerald-500 text-black" : "border-white/30 bg-white/5"
              }`}
            >
              {allowExternal && <Check size={10} strokeWidth={3} />}
            </span>
            <span>Allow external sites</span>
          </label>
        </div>
      </div>

      {error ? (
        <p
          role="alert"
          className="mx-3 my-2 rounded-lg border border-rose-400/20 bg-rose-500/10 px-2.5 py-1.5 text-xs leading-4 text-rose-300"
        >
          {error}
        </p>
      ) : null}

      {/* Address Sub-bar */}
      <div className="flex items-center gap-2 border-b border-white/10 bg-[#0e1311] px-3 py-1.5 text-[10px]">
        <div className="flex items-center gap-1.5 min-w-0 flex-1">
          <span
            className={`h-1.5 w-1.5 rounded-full shrink-0 ${
              loadedUrl ? "bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.6)]" : "bg-white/30"
            }`}
          />
          <span className="truncate font-mono text-white/60" title={loadedUrl ?? "No page loaded"}>
            {loadedUrl ?? "No page loaded"}
          </span>
        </div>
        {loadedUrl ? (
          <a
            href={loadedUrl}
            target="_blank"
            rel="noreferrer noopener"
            className="flex items-center gap-1 shrink-0 font-medium text-emerald-400 hover:text-emerald-300 transition"
          >
            <span>Open in new tab</span>
            <ExternalLink size={10} />
          </a>
        ) : null}
      </div>

      {/* Main Content Area */}
      <div className="min-h-0 flex-1 overflow-hidden bg-[#0a0d0c]">
        {loadedUrl ? (
          <iframe
            key={`${loadedUrl}#${reloadToken}`}
            title="Browser preview"
            src={loadedUrl}
            sandbox="allow-scripts allow-forms"
            className="h-full w-full border-0 bg-white"
          />
        ) : (
          <BrowserEmptyState
            notice={notice}
            onSelectUrl={(sample) => {
              setUrlInput(sample);
              const next = sample;
              setHistory((current) => [...current.slice(0, historyIndex + 1), next]);
              setHistoryIndex((current) => current + 1);
            }}
          />
        )}
      </div>
    </div>
  );
}

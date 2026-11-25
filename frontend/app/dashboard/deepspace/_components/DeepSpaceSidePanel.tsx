"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import { createPortal } from "react-dom";
import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import {
  AlignLeft,
  ArrowDown,
  ArrowUp,
  Briefcase,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Copy,
  ExternalLink,
  FileCode,
  FileText,
  Folder,
  FolderTree,
  Globe,
  History,
  Layout,
  LayoutList,
  Plus,
  RefreshCw,
  SquareTerminal,
  X,
  GitBranch,
  Package,
  Sparkles,
  AlertTriangle,
  CheckCircle,
  Maximize2,
  Minimize2,
  LayoutGrid,
} from "lucide-react";

import DeepSpaceJobsPanel from "./DeepSpaceJobsPanel";
import DeepSpaceTrajectoryView from "./DeepSpaceTrajectoryView";
import DeepSpaceStartCards from "./DeepSpaceStartCards";
import { fetchWithAuth } from "@/lib/api";
import { useRealtimeEvents } from "@/lib/realtime";
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
import { positionFloatingMenu } from "../_lib/floating-menu-position";
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
  syncBrowserStorageState,
  typeIntoBrowser,
} from "../_lib/browser-api";
import type { BrowserPageState } from "../_lib/browser-api";
import {
  SlotOutlet,
  SlotPanel,
  useModuleRegistry,
} from "@/app/dashboard/deepspace/_lib/module-registry";
import { BUILTIN_PANEL_TAB_IDS } from "@/app/dashboard/deepspace/_lib/builtin-modules";
import CodeBlock, { renderHighlightedLineTokens } from "../../query/_components/CodeBlock";
import DeepSpaceMarkdownRenderer from "./DeepSpaceMarkdownRenderer";
import type { AgentStep, DeepSpacePreviewRequest } from "../_lib/deepspace-stream";

/** The tools the side panel offers next to the DeepSpace chat. */
export type DeepSpaceSidePanelTab =
  | "start"
  | "files"
  | "terminal"
  | "browser"
  | "jobs"
  | "workflows"
  | "trajectory"
  | "deliverables";

/** One open tab instance inside the side panel. */
export interface OpenTab {
  /** Stable id for this tab instance. */
  id: string;
  /** The kind of tab (files, terminal, browser, …). */
  type: DeepSpaceSidePanelTab | "file" | "preview";
  /** Human-readable label shown on the tab. */
  label: string;
  /** Loaded file content for a review tab. */
  file?: WorkspaceFile;
  /** Virtual unsaved file or visual preview payload for a preview tab. */
  preview?: DeepSpacePreviewRequest;
  /** Conversation workspace directory, used only by the verified host-app API. */
  workspaceRoot?: string;
  /** Whether a file review tab should start on the unified diff view. */
  preferDiff?: boolean;
  /** Whether this terminal tab should create a new PTY session on mount. */
  startTerminal?: boolean;
}

export type FileDiffMap = Record<string, NonNullable<AgentStep["diffStats"]>>;

/** Props for the DeepSpaceSidePanel component. */
export interface DeepSpaceSidePanelProps {
  /** The conversation whose workspace, terminals, and browser preview are shown. */
  conversationId: string | null;
  /** The workspace in force for the conversation. */
  workspace?: Workspace | null;
  /** Whether the panel is visible. The panel renders nothing while it is closed. */
  open: boolean;
  /** Called when the user closes the panel. */
  onClose: () => void;
  /** The tab shown when the panel opens. Defaults to the Start tab. */
  initialTab?: DeepSpaceSidePanelTab;
  /** The tab the panel shows. A change moves the panel to that tab. */
  tab?: DeepSpaceSidePanelTab;
  /** Called when the user picks another tab. */
  onTabChange?: (tab: DeepSpaceSidePanelTab) => void;
  /** The shell a new terminal session runs under when the tab opens. */
  terminalShell?: TerminalShell;
  /** Whether the terminal tab opens a session of its own when it appears. */
  startTerminal?: boolean;
  /** Per-path diffs captured during the active conversation. */
  fileDiffs?: FileDiffMap;
  /** Imperative request to open a specific file review/diff tab. */
  requestedFileDiff?: { path: string; seq: number; preferDiff?: boolean } | null;
  /** Imperative request to open an in-memory file or visual preview tab. */
  requestedPreview?: DeepSpacePreviewRequest | null;
}

const TAB_LABELS: Record<DeepSpaceSidePanelTab, string> = {
  start: "Start",
  files: "Files",
  terminal: "Terminal",
  browser: "Browser",
  jobs: "Jobs",
  workflows: "Workflows",
  trajectory: "Trajectory",
  deliverables: "Deliverables",
};

let nextTabInstance = 0;
function nextTabId(): string {
  nextTabInstance += 1;
  return `side-panel-tab-${nextTabInstance}`;
}

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const MIN_PANEL_WIDTH = 22; // 22rem minimum
const MAX_PANEL_WIDTH = 64; // 64rem maximum
const DEFAULT_PANEL_WIDTH = 36; // 36rem gives file and tool surfaces room to breathe
const PANEL_WIDTH_STORAGE_KEY = "averqel_deepspace_side_panel_width_v2";
const LEGACY_PANEL_WIDTH_STORAGE_KEY = "averqel_deepspace_side_panel_width";

/** The message the server sends when a conversation holds no browser session. */
const MISSING_SESSION_MESSAGE = "No such browser session.";

type WorkflowRun = {
  run_id: string | number;
  workflow: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  current_phase: string | null;
  stop_reason: string | null;
  error: string | null;
  events: WorkflowEvent[];
  /** The durable job's id, which differs from the run id only for legacy rows. */
  job_id: string | null;
  /** Whether the run came from a document or from an inline script. */
  mode: string | null;
  /** Whether a cancellation was requested for the run. */
  cancel_requested: boolean;
  /** How many child agents the run started. */
  agents_spawned: number;
  /** The run's final value, when it completed. */
  result: Record<string, unknown> | null;
  /** The run's final value rendered as text, when it completed. */
  output: string | null;
  /** When the run was created. */
  created_at: string | null;
};

type WorkflowEvent = {
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string | null;
};

/**
 * Merge a server snapshot with the events already shown, so an event that
 * arrived over the realtime stream is not dropped by a later poll.
 */
function mergeWorkflowEvents(snapshot: WorkflowEvent[], live: WorkflowEvent[]): WorkflowEvent[] {
  const merged = new Map<number, WorkflowEvent>();
  for (const event of snapshot) merged.set(event.sequence, event);
  for (const event of live) {
    const existing = merged.get(event.sequence);
    if (!existing) {
      merged.set(event.sequence, event);
      continue;
    }
    merged.set(event.sequence, {
      ...existing,
      ...event,
      payload: { ...existing.payload, ...event.payload },
    });
  }
  return [...merged.values()].sort((left, right) => left.sequence - right.sequence);
}

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
    throw new Error(
      `${label} returned an invalid response. Restart the DeepSpace API and try again.`,
    );
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
    return [
      {
        run_id: item.run_id,
        workflow: item.workflow,
        status: item.status,
        started_at: nullableString(item.started_at),
        completed_at: nullableString(item.completed_at),
        current_phase: nullableString(item.current_phase),
        stop_reason: nullableString(item.stop_reason),
        error: nullableString(item.error),
        job_id: nullableString(item.job_id),
        mode: nullableString(item.mode),
        cancel_requested: item.cancel_requested === true,
        agents_spawned: typeof item.agents_spawned === "number" ? item.agents_spawned : 0,
        result: isRecord(item.result) ? item.result : null,
        output: nullableString(item.output),
        created_at: nullableString(item.created_at),
        events: Array.isArray(item.events)
          ? item.events.flatMap((event): WorkflowEvent[] => {
              if (
                !isRecord(event) ||
                typeof event.sequence !== "number" ||
                typeof event.type !== "string"
              )
                return [];
              return [
                {
                  sequence: event.sequence,
                  type: event.type,
                  payload: isRecord(event.payload) ? event.payload : {},
                  created_at: nullableString(event.created_at),
                },
              ];
            })
          : [],
      },
    ];
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
                return [
                  {
                    tool_name: tool.tool_name,
                    status: tool.status,
                    duration_ms: nullableNumber(tool.duration_ms),
                  },
                ];
              })
            : [];
          return [
            {
              step: step.step,
              duration_ms: nullableNumber(step.duration_ms),
              tools,
            },
          ];
        })
      : [];
    return [
      {
        turn: turn.turn,
        duration_ms: nullableNumber(turn.duration_ms),
        steps,
      },
    ];
  });
  return { turns };
}

function parseDeliverables(payload: unknown): DeliverablesData {
  if (!isRecord(payload)) throw new Error("Deliverables response was invalid");
  const changedFiles = Array.isArray(payload.changed_files)
    ? payload.changed_files.flatMap((file): ChangedFile[] => {
        if (!isRecord(file) || typeof file.path !== "string") return [];
        return [
          {
            path: file.path,
            write_count: typeof file.write_count === "number" ? file.write_count : 0,
          },
        ];
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
  initialTab = "start",
  tab,
  onTabChange,
  terminalShell,
  startTerminal,
  fileDiffs,
  requestedFileDiff,
  requestedPreview,
}: DeepSpaceSidePanelProps) {
  const [mounted, setMounted] = useState(open);
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (open) {
      setMounted(true);
      return;
    }
    const exitDuration = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 1 : 200;
    const timer = window.setTimeout(() => setMounted(false), exitDuration);
    return () => window.clearTimeout(timer);
  }, [open]);
  /* eslint-enable react-hooks/set-state-in-effect */
  if (!mounted) return null;

  return (
    <SidePanel
      conversationId={conversationId}
      workspace={workspace}
      initialTab={initialTab}
      tab={tab}
      onTabChange={onTabChange}
      terminalShell={terminalShell}
      startTerminal={startTerminal}
      fileDiffs={fileDiffs}
      requestedFileDiff={requestedFileDiff}
      requestedPreview={requestedPreview}
      closing={!open}
    />
  );
}

interface SidePanelProps {
  /** The conversation whose workspace, terminals, and browser preview are shown. */
  conversationId: string | null;
  /** The workspace in force for the conversation. */
  workspace?: Workspace | null;
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
  fileDiffs?: FileDiffMap;
  requestedFileDiff?: { path: string; seq: number; preferDiff?: boolean } | null;
  requestedPreview?: DeepSpacePreviewRequest | null;
  closing?: boolean;
}

/** The open panel. It is mounted fresh every time the panel opens. */
function SidePanel({
  conversationId,
  workspace,
  initialTab = "start",
  tab,
  onTabChange,
  terminalShell,
  startTerminal,
  fileDiffs,
  requestedFileDiff,
  requestedPreview = null,
  closing = false,
}: SidePanelProps) {
  const controlledType = tab;
  const [initialOpenTab] = useState<OpenTab>(() => {
    const type = controlledType ?? initialTab;
    return {
      id: nextTabId(),
      type,
      label: TAB_LABELS[type],
      startTerminal: type === "terminal" && Boolean(startTerminal),
    };
  });
  const [openTabs, setOpenTabs] = useState<OpenTab[]>([initialOpenTab]);
  const [userActiveTabId, setUserActiveTabId] = useState(initialOpenTab.id);
  const [activeTabByPane, setActiveTabByPane] = useState<Map<number, string>>(
    () => new Map([[0, initialOpenTab.id]]),
  );
  const [activePaneIndex, setActivePaneIndex] = useState(0);
  const [activeTerminalShell, setActiveTerminalShell] = useState<TerminalShell>(
    terminalShell ?? "bash",
  );

  // Split panes keep an active tab per pane, so both sides remain useful.
  const [paneAssignments, setPaneAssignments] = useState<Map<string, number>>(new Map());
  const [paneCount, setPaneCount] = useState(1);
  const [splitDirection, setSplitDirection] = useState<"horizontal" | "vertical">("horizontal");

  // Fullscreen mode
  const [isFullscreen, setIsFullscreen] = useState(false);

  // Editable tab title
  const [editingTabId, setEditingTabId] = useState<string | null>(null);
  const [tabTitleDraft, setTabTitleDraft] = useState("");

  // The verified host application catalog backs the file tab's "Open with" menu.
  const [openWithMenuOpen, setOpenWithMenuOpen] = useState(false);
  const [openWithMenuPosition, setOpenWithMenuPosition] = useState<{
    top: number;
    left: number;
    workspaceRoot: string;
  } | null>(null);
  const [openWithApps, setOpenWithApps] = useState<
    Array<{ id: string; name: string; category: string }>
  >([]);
  const [openWithLoading, setOpenWithLoading] = useState(false);
  const [openWithError, setOpenWithError] = useState<string | null>(null);
  const [tabPickerOpen, setTabPickerOpen] = useState(false);

  // Publish the conversation to every registered slot, so a module-provided
  // panel or toolbar action can read it through `useSlotContext`.
  const registry = useModuleRegistry();
  useEffect(() => {
    registry.setContext({ conversationId: conversationId ?? null });
  }, [registry, conversationId]);

  const previousControlledType = useRef(controlledType);
  // Parent-driven navigation opens or selects a surface only when the value
  // changes. File review tabs remain locally selectable while the parent
  // continues to report the last tool surface.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (previousControlledType.current === controlledType) return;
    previousControlledType.current = controlledType;
    if (!controlledType) return;
    const existing = openTabs.find(
      (t) => t.type === controlledType && (t.type !== "terminal" || t.id === userActiveTabId),
    );
    if (existing) {
      const paneIndex = paneAssignments.get(existing.id) ?? 0;
      setUserActiveTabId(existing.id);
      setActivePaneIndex(paneIndex);
      setActiveTabByPane((current) => new Map(current).set(paneIndex, existing.id));
      return;
    }
    const newId = nextTabId();
    const newTab: OpenTab = {
      id: newId,
      type: controlledType,
      label: TAB_LABELS[controlledType],
      startTerminal: controlledType === "terminal" && Boolean(startTerminal),
    };
    setOpenTabs((current) => [...current, newTab]);
    setPaneAssignments((current) => new Map(current).set(newId, 0));
    setUserActiveTabId(newId);
    setActivePaneIndex(0);
    setActiveTabByPane((current) => new Map(current).set(0, newId));
  }, [controlledType, openTabs, paneAssignments, startTerminal, userActiveTabId]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const switchTab = (tabId: string) => {
    setUserActiveTabId(tabId);
    const selected = openTabs.find((x) => x.id === tabId);
    if (!selected) return;
    const paneIndex = paneAssignments.get(tabId) ?? 0;
    setActivePaneIndex(paneIndex);
    setActiveTabByPane((current) => new Map(current).set(paneIndex, tabId));
    if (selected.type !== "file" && selected.type !== "preview") onTabChange?.(selected.type);
  };

  const addTab = (type: DeepSpaceSidePanelTab, label?: string, startNewTerminal = false) => {
    const existing = type === "terminal" ? undefined : openTabs.find((t) => t.type === type);
    if (existing) {
      setUserActiveTabId(existing.id);
      const paneIndex = paneAssignments.get(existing.id) ?? activePaneIndex;
      setActivePaneIndex(paneIndex);
      setActiveTabByPane((current) => new Map(current).set(paneIndex, existing.id));
      setTabPickerOpen(false);
      onTabChange?.(type);
      return;
    }
    const newId = nextTabId();
    const newTab: OpenTab = {
      id: newId,
      type,
      label: label ?? (type === "terminal" ? activeTerminalShell : TAB_LABELS[type]),
      startTerminal: type === "terminal" && startNewTerminal,
    };
    const paneIndex = activePaneIndex;
    setOpenTabs((current) => [...current, newTab]);
    setPaneAssignments((current) => new Map(current).set(newId, paneIndex));
    setActiveTabByPane((current) => new Map(current).set(paneIndex, newId));
    setUserActiveTabId(newId);
    setTabPickerOpen(false);
    if (type !== "terminal") onTabChange?.(type);
  };

  const lookupDiffForPath = (filePath: string) => {
    if (!fileDiffs || !filePath) return undefined;
    if (fileDiffs[filePath]) return fileDiffs[filePath];
    const trimmed = filePath.replace(/^\.\//, "");
    if (fileDiffs[trimmed]) return fileDiffs[trimmed];
    return Object.values(fileDiffs).find(
      (entry) =>
        entry.path === filePath ||
        entry.path.replace(/^\.\//, "") === trimmed ||
        entry.path.endsWith(`/${trimmed}`) ||
        trimmed.endsWith(`/${entry.path.replace(/^\.\//, "")}`),
    );
  };

  const openFileTab = (
    file: WorkspaceFile,
    workspaceRoot: string,
    opts?: { preferDiff?: boolean },
  ) => {
    const preferDiff = opts?.preferDiff ?? Boolean(lookupDiffForPath(file.path));
    const existing = openTabs.find((t) => t.type === "file" && t.file?.path === file.path);
    if (existing) {
      setOpenTabs((current) =>
        current.map((t) =>
          t.id === existing.id ? { ...t, file, workspaceRoot, preferDiff } : t,
        ),
      );
      switchTab(existing.id);
      return;
    }
    const id = nextTabId();
    const basename = file.path.split(/[\\/]/).filter(Boolean).at(-1) ?? file.path;
    const reviewTab: OpenTab = {
      id,
      type: "file",
      label: `Review · ${basename}`,
      file,
      workspaceRoot,
      preferDiff,
    };
    const paneIndex = paneAssignments.get(userActiveTabId) ?? activePaneIndex;
    setOpenTabs((current) => [...current, reviewTab]);
    setPaneAssignments((current) => new Map(current).set(id, paneIndex));
    setActivePaneIndex(paneIndex);
    setActiveTabByPane((current) => new Map(current).set(paneIndex, id));
    setUserActiveTabId(id);
  };

  const openPathInReview = async (targetPath: string, preferDiff = true) => {
    if (!conversationId || !targetPath) return;
    const root = workspace?.path ?? "";
    const diffMatch = lookupDiffForPath(targetPath);
    try {
      const loaded = await readWorkspaceFile(conversationId, targetPath);
      openFileTab(loaded, root, { preferDiff });
    } catch {
      if (diffMatch) {
        const reconstructed = (diffMatch.diffLines ?? [])
          .filter((line) => line.type !== "removed")
          .map((line) => line.text)
          .join("\n");
        openFileTab(
          {
            path: targetPath,
            content: reconstructed,
            truncated: false,
          } as WorkspaceFile,
          root,
          { preferDiff: true },
        );
      }
    }
  };

  const lastOpenedSeqRef = useRef<number | null>(null);
  useEffect(() => {
    if (!requestedFileDiff || requestedFileDiff.seq === lastOpenedSeqRef.current) return;
    lastOpenedSeqRef.current = requestedFileDiff.seq;
    void openPathInReview(requestedFileDiff.path, requestedFileDiff.preferDiff ?? true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedFileDiff?.seq, requestedFileDiff?.path]);

  const openPreviewTab = (preview: DeepSpacePreviewRequest) => {
    const existing = openTabs.find(
      (t) =>
        (t.type === "preview" && t.preview?.id === preview.id) ||
        (preview.path && t.type === "preview" && t.preview?.path === preview.path),
    );
    if (existing) {
      setOpenTabs((current) =>
        current.map((t) =>
          t.id === existing.id ? { ...t, label: preview.title, preview } : t,
        ),
      );
      switchTab(existing.id);
      return;
    }
    const id = nextTabId();
    const previewTab: OpenTab = {
      id,
      type: "preview",
      label: preview.title,
      preview,
    };
    const paneIndex = paneAssignments.get(userActiveTabId) ?? activePaneIndex;
    setOpenTabs((current) => [...current, previewTab]);
    setPaneAssignments((current) => new Map(current).set(id, paneIndex));
    setActivePaneIndex(paneIndex);
    setActiveTabByPane((current) => new Map(current).set(paneIndex, id));
    setUserActiveTabId(id);
  };

  const lastOpenedPreviewSeqRef = useRef<number | null>(null);
  useEffect(() => {
    if (!requestedPreview || requestedPreview.seq === lastOpenedPreviewSeqRef.current) return;
    lastOpenedPreviewSeqRef.current = requestedPreview.seq;
    openPreviewTab(requestedPreview);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedPreview?.seq, requestedPreview?.id]);

  const closeTab = (tabId: string) => {
    const closing = openTabs.find((t) => t.id === tabId);
    if (closing?.type === "browser") {
      void window.electron?.browserView?.close(closing.id);
    }
    if (!closing) return;
    const paneIndex = paneAssignments.get(tabId) ?? 0;
    const remainingTabs = openTabs.filter((t) => t.id !== tabId);
    const remainingInPane = remainingTabs.filter(
      (t) => (paneAssignments.get(t.id) ?? 0) === paneIndex,
    );
    // A split is only useful while both panes have a tab. Collapse it as soon
    // as either pane becomes empty, and move remaining tabs into pane zero.
    // Keeping a new Start tab assigned to a removed pane leaves its tab
    // highlighted while the old pane keeps rendering stale content.
    const collapseSplit = paneCount > 1 && remainingInPane.length === 0;
    const startTab =
      remainingTabs.length === 0
        ? { id: nextTabId(), type: "start" as const, label: TAB_LABELS.start }
        : null;
    const nextTabs = [...remainingTabs, ...(startTab ? [startTab] : [])];
    const nextPaneAssignments = new Map<string, number>();
    for (const remainingTab of remainingTabs) {
      nextPaneAssignments.set(
        remainingTab.id,
        collapseSplit ? 0 : (paneAssignments.get(remainingTab.id) ?? 0),
      );
    }
    if (startTab) nextPaneAssignments.set(startTab.id, 0);

    const wasGlobalActive = userActiveTabId === tabId;
    const samePaneFallback = remainingInPane.at(-1);
    const nextActiveTab = wasGlobalActive
      ? (samePaneFallback ?? remainingTabs.at(-1) ?? startTab)
      : (openTabs.find((t) => t.id === userActiveTabId) ?? remainingTabs.at(-1) ?? startTab);
    const nextActiveTabId = nextActiveTab?.id;
    const nextActivePane = nextActiveTabId ? (nextPaneAssignments.get(nextActiveTabId) ?? 0) : 0;

    setOpenTabs(nextTabs);
    setPaneAssignments(nextPaneAssignments);
    if (collapseSplit) {
      setPaneCount(1);
      setActivePaneIndex(0);
      setActiveTabByPane(nextActiveTabId ? new Map([[0, nextActiveTabId]]) : new Map());
    } else {
      setActiveTabByPane((current) => {
        const next = new Map(current);
        if (next.get(paneIndex) === tabId) {
          const paneFallback = samePaneFallback ?? startTab;
          if (paneFallback) next.set(paneIndex, paneFallback.id);
          else next.delete(paneIndex);
        }
        if (wasGlobalActive && nextActiveTabId) {
          next.set(nextActivePane, nextActiveTabId);
        }
        return next;
      });
      if (wasGlobalActive && nextActiveTabId) setActivePaneIndex(nextActivePane);
    }
    if (wasGlobalActive && nextActiveTabId) {
      setUserActiveTabId(nextActiveTabId);
      if (nextActiveTab && nextActiveTab.type !== "file" && nextActiveTab.type !== "preview") {
        onTabChange?.(nextActiveTab.type);
      }
    }
    if (closing?.type === "file") setOpenWithError(null);
  };

  // Duplicate the active surface into a second pane; clicking again collapses it.
  const splitActiveTab = () => {
    const active = openTabs.find((t) => t.id === userActiveTabId);
    if (!active) return;
    if (paneCount > 1) {
      setPaneCount(1);
      setPaneAssignments((current) => new Map([...current].map(([id]) => [id, 0])));
      setActiveTabByPane(new Map([[0, userActiveTabId]]));
      setActivePaneIndex(0);
      return;
    }
    const duplicateId = nextTabId();
    const duplicate: OpenTab = {
      ...active,
      id: duplicateId,
      label: `${active.label} · 2`,
      startTerminal: false,
    };
    setOpenTabs((current) => [...current, duplicate]);
    setPaneAssignments((current) => new Map(current).set(duplicateId, 1));
    setActiveTabByPane((current) => new Map(current).set(1, duplicateId));
    setPaneCount(2);
    setActivePaneIndex(1);
    setUserActiveTabId(duplicateId);
  };

  const openWithWorkspace = async (button: HTMLButtonElement, workspaceRoot: string) => {
    const bounds = button.getBoundingClientRect();
    setOpenWithMenuPosition({
      top: bounds.bottom + 4,
      left: bounds.left,
      workspaceRoot,
    });
    setOpenWithMenuOpen(true);
    setOpenWithError(null);
    setOpenWithLoading(true);
    try {
      const response = await fetchWithAuth("/host/open-in-app/apps");
      if (!response.ok) {
        throw new Error(`Available applications could not be loaded (${response.status}).`);
      }
      const payload: unknown = await response.json();
      const apps = Array.isArray(payload)
        ? payload.flatMap((value) => {
            if (
              !isRecord(value) ||
              typeof value.id !== "string" ||
              typeof value.name !== "string"
            ) {
              return [];
            }
            return [
              {
                id: value.id,
                name: value.name,
                category: typeof value.category === "string" ? value.category : "app",
              },
            ];
          })
        : [];
      setOpenWithApps(apps);
      if (!apps.length) {
        setOpenWithError("No compatible desktop applications are available on this host.");
      }
    } catch (cause) {
      setOpenWithApps([]);
      setOpenWithError(errorMessage(cause, "Available applications could not be loaded."));
    } finally {
      setOpenWithLoading(false);
    }
  };

  const launchWithApp = async (appId: string, workspaceRoot: string) => {
    try {
      const response = await fetchWithAuth("/host/open-in-app/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app_id: appId, path: workspaceRoot }),
      });
      const result: unknown = await response.json();
      if (!response.ok || !isRecord(result) || result.success !== true) {
        throw new Error("The selected application could not open this workspace.");
      }
      setOpenWithMenuOpen(false);
      setOpenWithError(null);
    } catch (cause) {
      setOpenWithError(
        errorMessage(cause, "The selected application could not open this workspace."),
      );
    }
  };

  // Toggle fullscreen mode
  const toggleFullscreen = () => {
    setIsFullscreen((prev) => !prev);
  };

  // Start editing a tab title
  const startEditTabTitle = (tabId: string) => {
    const tab = openTabs.find((t) => t.id === tabId);
    if (!tab) return;
    setTabTitleDraft(tab.label);
    setEditingTabId(tabId);
  };

  const finishEditTabTitle = () => {
    if (!editingTabId) return;
    setOpenTabs((current) =>
      current.map((t) => (t.id === editingTabId ? { ...t, label: tabTitleDraft || t.label } : t)),
    );
    setEditingTabId(null);
    setTabTitleDraft("");
  };

  const cancelEditTabTitle = () => {
    setEditingTabId(null);
    setTabTitleDraft("");
  };

  // Get tabs grouped by pane
  const tabsByPane = useMemo(() => {
    const panes: OpenTab[][] = Array.from({ length: paneCount }, () => []);
    for (const tab of openTabs) {
      const pane = paneAssignments.get(tab.id) ?? 0;
      if (pane >= 0 && pane < paneCount) panes[pane].push(tab);
    }
    return panes;
  }, [openTabs, paneAssignments, paneCount]);
  // Resizable panel width state
  const [panelWidth, setPanelWidth] = useState(DEFAULT_PANEL_WIDTH);
  const [isResizing, setIsResizing] = useState(false);
  const resizeHandleRef = useRef<HTMLDivElement | null>(null);
  const resizeStartRef = useRef<{ mouseX: number; startWidth: number } | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const currentWidthRef = useRef<number>(panelWidth);

  // The selected workspace changes the ownership boundary for this cached
  // tree, so clear its local previews before loading the next workspace.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    currentWidthRef.current = panelWidth;
  }, [panelWidth]);

  // Load saved width on mount
  useEffect(() => {
    const currentWidth = localStorage.getItem(PANEL_WIDTH_STORAGE_KEY);
    const legacyWidth = localStorage.getItem(LEGACY_PANEL_WIDTH_STORAGE_KEY);
    const savedWidth = currentWidth ?? legacyWidth;
    if (savedWidth) {
      const parsed = parseFloat(savedWidth);
      if (!isNaN(parsed) && parsed >= MIN_PANEL_WIDTH && parsed <= MAX_PANEL_WIDTH) {
        // The previous 28rem default made this panel look like a narrow
        // utility drawer. Preserve explicit user sizing, but upgrade that
        // old default to the new workbench width.
        const migrated = currentWidth === null && parsed === 28 ? DEFAULT_PANEL_WIDTH : parsed;
        queueMicrotask(() => {
          setPanelWidth(migrated);
          currentWidthRef.current = migrated;
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
        const clampedWidth = Math.min(
          Math.max(rawWidth, MIN_PANEL_WIDTH),
          Math.max(MIN_PANEL_WIDTH, maxAllowedRem),
        );

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
      className={`deepspace-side-panel border-border-subtle bg-surface-2 relative flex h-full max-w-full min-w-0 shrink-0 flex-col overflow-hidden rounded-xl border transition-[transform,opacity,width] duration-200 ease-out ${closing ? "translate-x-full opacity-0" : "translate-x-0 opacity-100"}`}
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
        className={`group absolute top-0 bottom-0 -left-1.5 z-30 flex w-3 cursor-col-resize items-center justify-center transition-colors select-none ${
          isResizing ? "bg-primary/20" : "hover:bg-primary/10"
        }`}
        aria-label="Resize panel"
        title="Drag to resize panel"
      >
        <div
          className={`h-full w-[2px] transition-colors ${
            isResizing
              ? "bg-primary shadow-[0_0_8px_rgba(var(--primary),0.8)]"
              : "group-hover:bg-primary/70 bg-transparent"
          }`}
        />
      </div>

      <TabBar
        tabs={openTabs}
        activeTabId={userActiveTabId}
        onTabSwitch={switchTab}
        onTabClose={closeTab}
        onAddTab={(type, startNewTerminal) => addTab(type, undefined, startNewTerminal)}
        tabPickerOpen={tabPickerOpen}
        onTabPickerToggle={() => setTabPickerOpen((value) => !value)}
        onTabPickerClose={() => setTabPickerOpen(false)}
        paneCount={paneCount}
        onToggleSplit={splitActiveTab}
        splitDirection={splitDirection}
        onToggleSplitDirection={() =>
          setSplitDirection((value) => (value === "horizontal" ? "vertical" : "horizontal"))
        }
        isFullscreen={isFullscreen}
        onToggleFullscreen={toggleFullscreen}
        editingTabId={editingTabId}
        tabTitleDraft={tabTitleDraft}
        onEditTabTitleChange={setTabTitleDraft}
        onStartEditTabTitle={startEditTabTitle}
        onFinishEditTabTitle={finishEditTabTitle}
        onCancelEditTabTitle={cancelEditTabTitle}
      />

      <div
        className={`min-h-0 flex-1 overflow-hidden ${isFullscreen ? "bg-surface-2 fixed inset-0 z-[50]" : ""}`}
        style={{
          display: "flex",
          flexDirection: splitDirection === "horizontal" ? "row" : "column",
        }}
      >
        {tabsByPane.map((paneTabs, paneIndex) => {
          const paneTabId = activeTabByPane.get(paneIndex) ?? paneTabs[0]?.id;
          const paneTab = paneTabs.find((entry) => entry.id === paneTabId) ?? paneTabs[0];
          if (!paneTab) {
            return (
              <div
                key={paneIndex}
                className="border-border-subtle/50 flex min-h-0 min-w-0 flex-1 items-center justify-center border-r"
              >
                <span className="text-muted-foreground text-xs">Open a tab in this pane.</span>
              </div>
            );
          }
          return (
            <div
              key={paneIndex}
              className="deepspace-workbench-surface border-border-subtle/50 flex min-h-0 min-w-0 flex-1 flex-col border-r last:border-0"
              role="tabpanel"
              id={`deepspace-side-panel-panel-${paneTab.id}`}
              aria-labelledby={`deepspace-side-panel-tab-${paneTab.id}`}
              tabIndex={0}
            >
              {paneTab.type === "start" ? (
                <div className="flex h-full min-h-0 w-full items-center justify-center overflow-y-auto p-5 sm:p-8">
                  <div className="w-full max-w-[24rem]">
                    <div className="mb-7 text-center">
                      <div className="border-border-subtle bg-surface-1 text-muted-foreground mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full border">
                        <LayoutGrid size={21} />
                      </div>
                      <h2 className="text-sm font-semibold">Start</h2>
                      <p className="text-muted-foreground mt-1 text-xs">
                        Choose a workspace surface to begin.
                      </p>
                    </div>
                    <DeepSpaceStartCards
                      disabled={!conversationId}
                      onOpenFiles={() => addTab("files")}
                      onOpenTerminal={(shell) => {
                        setActiveTerminalShell(shell);
                        addTab("terminal", undefined, true);
                      }}
                      onOpenBrowser={() => addTab("browser")}
                      onOpenMoreTools={() => setTabPickerOpen(true)}
                    />
                  </div>
                </div>
              ) : null}
              {paneTab.type === "files" ? (
                <FilesTab
                  key={`${conversationId ?? "no-conversation"}:${workspace?.id ?? "default"}:${paneTab.id}`}
                  conversationId={conversationId}
                  workspace={workspace}
                  onFileOpen={(file, workspaceRoot) => openFileTab(file, workspaceRoot)}
                />
              ) : null}
              {paneTab.type === "file" && paneTab.file ? (
                <FileReviewTab
                  key={`${paneTab.id}:${paneTab.preferDiff ? "diff" : "file"}`}
                  file={paneTab.file}
                  workspaceRoot={paneTab.workspaceRoot ?? workspace?.path ?? ""}
                  diffStats={lookupDiffForPath(paneTab.file.path)}
                  initialMode={
                    paneTab.preferDiff && lookupDiffForPath(paneTab.file.path) ? "diff" : "file"
                  }
                  openWithError={openWithError}
                  onOpenWith={(button, workspaceRoot) =>
                    void openWithWorkspace(button, workspaceRoot)
                  }
                  onRefresh={async () => {
                    if (!conversationId) return;
                    const file = await readWorkspaceFile(conversationId, paneTab.file!.path);
                    setOpenTabs((current) =>
                      current.map((entry) =>
                        entry.id === paneTab.id ? { ...entry, file } : entry,
                      ),
                    );
                  }}
                />
              ) : null}
              {paneTab.type === "terminal" ? (
                <TerminalTab
                  key={`${conversationId ?? "no-conversation"}:${paneTab.id}`}
                  conversationId={conversationId}
                  initialShell={activeTerminalShell}
                  startSession={paneTab.startTerminal}
                />
              ) : null}
              {paneTab.type === "browser" ? (
                <BrowserTab
                  key={`${conversationId ?? "no-conversation"}:${paneTab.id}`}
                  conversationId={conversationId}
                  viewId={paneTab.id}
                />
              ) : null}
              {paneTab.type === "jobs" ? (
                <DeepSpaceJobsPanel key={paneTab.id} conversationId={conversationId} />
              ) : null}
              {paneTab.type === "workflows" ? (
                <WorkflowsTab key={paneTab.id} conversationId={conversationId} />
              ) : null}
              {paneTab.type === "trajectory" ? (
                <TrajectoryTab key={paneTab.id} conversationId={conversationId} />
              ) : null}
              {paneTab.type === "deliverables" ? (
                <DeliverablesTab
                  key={paneTab.id}
                  conversationId={conversationId}
                  fileDiffs={fileDiffs}
                  onOpenFile={(path) => void openPathInReview(path, true)}
                />
              ) : null}
              {paneTab.type === "preview" && paneTab.preview ? (
                <PreviewPanelTab
                  key={`${paneTab.id}:${paneTab.preview.id}`}
                  preview={paneTab.preview}
                />
              ) : null}
              {paneTab.type !== "file" &&
              paneTab.type !== "preview" &&
              !BUILTIN_PANEL_TAB_IDS.includes(paneTab.type) ? (
                <SlotPanel slot="panel-tab" activeTab={paneTab.type} />
              ) : null}
            </div>
          );
        })}
      </div>

      {/* The menu contains only applications verified by the local host. */}
      {openWithMenuOpen && openWithMenuPosition && (
        <OpenWithDropdown
          open={openWithMenuOpen}
          position={openWithMenuPosition}
          apps={openWithApps}
          loading={openWithLoading}
          error={openWithError}
          onAppSelect={launchWithApp}
          onClose={() => setOpenWithMenuOpen(false)}
        />
      )}
    </aside>
  );
}

/** Compact workbench tabs with pinned controls and an overflow-safe tab picker. */
function TabBar({
  tabs,
  activeTabId,
  onTabSwitch,
  onTabClose,
  onAddTab,
  tabPickerOpen,
  onTabPickerToggle,
  onTabPickerClose,
  paneCount,
  onToggleSplit,
  splitDirection,
  onToggleSplitDirection,
  isFullscreen,
  onToggleFullscreen,
  editingTabId,
  tabTitleDraft,
  onEditTabTitleChange,
  onStartEditTabTitle,
  onFinishEditTabTitle,
  onCancelEditTabTitle,
}: {
  tabs: OpenTab[];
  activeTabId: string;
  onTabSwitch: (tabId: string) => void;
  onTabClose: (tabId: string) => void;
  onAddTab: (type: DeepSpaceSidePanelTab, startNewTerminal?: boolean) => void;
  tabPickerOpen: boolean;
  onTabPickerToggle: () => void;
  onTabPickerClose: () => void;
  paneCount: number;
  onToggleSplit: () => void;
  splitDirection: "horizontal" | "vertical";
  onToggleSplitDirection: () => void;
  isFullscreen: boolean;
  onToggleFullscreen: () => void;
  editingTabId: string | null;
  tabTitleDraft: string;
  onEditTabTitleChange: (value: string) => void;
  onStartEditTabTitle: (tabId: string) => void;
  onFinishEditTabTitle: () => void;
  onCancelEditTabTitle: () => void;
}) {
  const icons: Record<
    DeepSpaceSidePanelTab | "file" | "preview",
    React.ComponentType<{ size?: number; className?: string }>
  > = {
    start: LayoutGrid,
    files: FolderTree,
    terminal: SquareTerminal,
    browser: Globe,
    jobs: Briefcase,
    workflows: GitBranch,
    trajectory: History,
    deliverables: Package,
    file: FileCode,
    preview: FileCode,
  };
  const menuTabs: DeepSpaceSidePanelTab[] = [
    "start",
    "files",
    "terminal",
    "browser",
    "jobs",
    "workflows",
    "trajectory",
    "deliverables",
  ];

  return (
    <div className="deepspace-workbench-tabbar border-border-subtle relative flex min-w-0 items-center gap-1 border-b px-2 py-1.5">
      <SlotOutlet slot="toolbar" className="flex shrink-0 items-center gap-1" />
      <div
        role="tablist"
        aria-label="Workspace tabs"
        className="scrollbar-hide flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
      >
        {tabs.map((tab) => {
          const Icon = icons[tab.type];
          const isActive = tab.id === activeTabId;
          const isEditing = editingTabId === tab.id;
          return (
            <div
              key={tab.id}
              className={`flex shrink-0 items-center gap-0.5 rounded-lg border px-1 ${isActive ? "border-border-subtle bg-surface-1 text-foreground" : "text-muted-foreground hover:bg-surface-1 border-transparent"}`}
            >
              {isEditing ? (
                <input
                  autoFocus
                  aria-label="Tab title"
                  value={tabTitleDraft}
                  onChange={(event) => onEditTabTitleChange(event.target.value)}
                  onBlur={onFinishEditTabTitle}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") onFinishEditTabTitle();
                    if (event.key === "Escape") onCancelEditTabTitle();
                  }}
                  className="w-28 bg-transparent px-2 py-1 text-[11px] outline-none"
                />
              ) : (
                <button
                  type="button"
                  role="tab"
                  id={`deepspace-side-panel-tab-${tab.id}`}
                  aria-selected={isActive}
                  aria-controls={`deepspace-side-panel-panel-${tab.id}`}
                  onClick={() => onTabSwitch(tab.id)}
                  onDoubleClick={() => onStartEditTabTitle(tab.id)}
                  title={tab.label}
                  className="inline-flex max-w-52 shrink-0 items-center gap-1.5 px-2 py-1.5 text-[11px] font-medium whitespace-nowrap"
                >
                  <Icon size={13} className="shrink-0" />
                  <span className="max-w-40 truncate">{tab.label}</span>
                </button>
              )}
              <button
                type="button"
                aria-label={`Close ${tab.label} tab`}
                title={`Close ${tab.label}`}
                onClick={() => onTabClose(tab.id)}
                className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-md p-1"
              >
                <X size={11} />
              </button>
            </div>
          );
        })}
      </div>

      <div className="border-border-subtle flex shrink-0 items-center gap-1 border-l pl-1">
        <button
          type="button"
          aria-label={paneCount > 1 ? "Close split view" : "Split active tab"}
          title={paneCount > 1 ? "Close split view" : "Split active tab"}
          aria-pressed={paneCount > 1}
          onClick={onToggleSplit}
          className={`rounded-md p-1.5 transition ${paneCount > 1 ? "bg-surface-1 text-foreground" : "text-muted-foreground hover:bg-surface-1 hover:text-foreground"}`}
        >
          <Layout size={14} />
        </button>
        {paneCount > 1 ? (
          <button
            type="button"
            aria-label="Switch split orientation"
            title="Switch split orientation"
            onClick={onToggleSplitDirection}
            className="text-muted-foreground hover:bg-surface-1 hover:text-foreground rounded-md p-1.5"
          >
            {splitDirection === "horizontal" ? <LayoutList size={14} /> : <Layout size={14} />}
          </button>
        ) : null}
        <button
          type="button"
          aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
          title={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
          aria-pressed={isFullscreen}
          onClick={onToggleFullscreen}
          className="text-muted-foreground hover:bg-surface-1 hover:text-foreground rounded-md p-1.5"
        >
          {isFullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
        </button>
        <button
          type="button"
          aria-label="Open a workspace tab"
          title="Open a workspace tab"
          aria-haspopup="menu"
          aria-expanded={tabPickerOpen}
          onClick={onTabPickerToggle}
          className="text-muted-foreground hover:bg-surface-1 hover:text-foreground rounded-md p-1.5"
        >
          <Plus size={14} />
        </button>
      </div>

      {tabPickerOpen ? (
        <>
          <button
            className="fixed inset-0 z-40 cursor-default"
            aria-label="Close tab menu"
            onClick={onTabPickerClose}
          />
          <div
            role="menu"
            aria-label="Open workspace tab"
            className="border-border-subtle bg-surface-1 absolute top-full right-2 z-50 mt-1 w-56 rounded-lg border p-1 shadow-xl"
          >
            {menuTabs.map((type) => {
              const Icon = icons[type];
              return (
                <button
                  key={type}
                  type="button"
                  role="menuitem"
                  onClick={() => onAddTab(type, type === "terminal")}
                  className="text-foreground hover:bg-surface-2 flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-xs"
                >
                  <Icon size={14} className="text-muted-foreground" />
                  <span>{TAB_LABELS[type]}</span>
                </button>
              );
            })}
          </div>
        </>
      ) : null}
    </div>
  );
}

function FileReviewTab({
  file,
  workspaceRoot,
  diffStats,
  initialMode = "file",
  openWithError,
  onOpenWith,
  onRefresh,
}: {
  file: WorkspaceFile;
  workspaceRoot: string;
  diffStats?: NonNullable<AgentStep["diffStats"]>;
  initialMode?: "diff" | "file";
  openWithError: string | null;
  onOpenWith: (button: HTMLButtonElement, workspaceRoot: string) => void;
  onRefresh: () => Promise<void>;
}) {
  const [lineWrap, setLineWrap] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"diff" | "file">(
    diffStats ? initialMode : "file",
  );
  const activeMode = diffStats ? viewMode : "file";
  const lineCount = Math.max(1, file.content.split("\n").length);

  const refresh = async () => {
    setRefreshing(true);
    setRefreshError(null);
    try {
      await onRefresh();
    } catch (cause) {
      setRefreshError(errorMessage(cause, "The file could not be refreshed."));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="bg-surface-0/40 flex h-full min-h-0 w-full flex-col">
      <div className="border-border-subtle flex min-w-0 items-center gap-2 border-b px-3 py-2">
        <div
          className="min-w-0 flex-1 truncate font-mono text-[11px]"
          title={`${workspaceRoot}/${file.path}`}
        >
          <span className="text-muted-foreground">
            {workspaceRoot.replace(/^\/home\/[^/]+/, "~")}/
          </span>
          <span className="text-foreground">{file.path}</span>
        </div>
        <button
          type="button"
          aria-label="Open workspace with an application"
          title="Open workspace with…"
          disabled={!workspaceRoot}
          onClick={(event) => onOpenWith(event.currentTarget, workspaceRoot)}
          className="text-muted-foreground hover:bg-surface-1 hover:text-foreground rounded-md p-1.5 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ExternalLink size={14} />
        </button>
        <button
          type="button"
          aria-label="Refresh file content"
          title="Refresh file content"
          disabled={refreshing}
          onClick={() => void refresh()}
          className="text-muted-foreground hover:bg-surface-1 hover:text-foreground rounded-md p-1.5 disabled:opacity-40"
        >
          <RefreshCw size={14} className={refreshing ? "animate-spin" : ""} />
        </button>
      </div>
      <div className="border-border-subtle/70 flex items-center gap-1.5 border-b px-2 py-1">
        {diffStats ? (
          <div
            role="tablist"
            aria-label="File review mode"
            className="border-border-subtle bg-surface-1/70 inline-flex items-center rounded-md border p-0.5 text-[10px]"
          >
            <button
              type="button"
              role="tab"
              aria-selected={activeMode === "diff"}
              onClick={() => setViewMode("diff")}
              className={`rounded px-2 py-0.5 font-medium transition ${
                activeMode === "diff"
                  ? "bg-surface-2 text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              Diff{" "}
              <span className="ml-1 font-mono text-emerald-400">+{diffStats.additions}</span>{" "}
              <span className="font-mono text-rose-400">-{diffStats.deletions}</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={activeMode === "file"}
              onClick={() => setViewMode("file")}
              className={`rounded px-2 py-0.5 font-medium transition ${
                activeMode === "file"
                  ? "bg-surface-2 text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              File
            </button>
          </div>
        ) : null}
        <button
          type="button"
          aria-label="Toggle line wrap"
          title={lineWrap ? "Disable line wrap" : "Enable line wrap"}
          aria-pressed={lineWrap}
          onClick={() => setLineWrap((current) => !current)}
          className={`rounded-md p-1.5 transition ${lineWrap ? "bg-surface-1 text-foreground" : "text-muted-foreground hover:bg-surface-1 hover:text-foreground"}`}
        >
          <AlignLeft size={14} />
        </button>
        <span className="text-muted-foreground ml-auto text-[10px]">
          {file.language ?? "Plain text"}
          {file.truncated ? " · Preview truncated" : ""}
        </span>
      </div>
      {refreshError || openWithError ? (
        <p
          role="alert"
          className="border-b border-rose-500/20 bg-rose-500/5 px-3 py-2 text-xs text-rose-400"
        >
          {refreshError ?? openWithError}
        </p>
      ) : null}
      {activeMode === "diff" && diffStats ? (
        <div
          className="custom-scrollbar min-h-0 flex-1 overflow-auto font-mono text-[11.5px] leading-5"
          data-testid="deepspace-side-panel-diff"
        >
          {diffStats.diffLines && diffStats.diffLines.length > 0 ? (
            <table className="w-full border-collapse">
              <tbody>
                {diffStats.diffLines.map((line, idx) => {
                  const isAdded = line.type === "added";
                  const isRemoved = line.type === "removed";
                  return (
                    <tr
                      key={`${diffStats.path}-panel-${idx}`}
                      className={
                        isAdded
                          ? "bg-emerald-500/[0.10]"
                          : isRemoved
                            ? "bg-rose-500/[0.10]"
                            : ""
                      }
                    >
                      <td className="text-muted-foreground/50 border-border-subtle/40 w-9 border-r px-2 text-right select-none">
                        {idx + 1}
                      </td>
                      <td
                        className={`w-5 px-1.5 text-center select-none ${
                          isAdded
                            ? "text-emerald-400"
                            : isRemoved
                              ? "text-rose-400"
                              : "text-muted-foreground/40"
                        }`}
                      >
                        {line.type === "unchanged" ? " " : isAdded ? "+" : "-"}
                      </td>
                      <td
                        className={`px-3 ${
                          lineWrap ? "break-words whitespace-pre-wrap" : "whitespace-pre"
                        }`}
                      >
                        {line.text
                          ? renderHighlightedLineTokens(
                              line.text,
                              file.language ??
                                (file.path.split(".").pop()?.toLowerCase() || "typescript"),
                            )
                          : " "}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <p className="text-muted-foreground p-4 text-xs">No line-level diff recorded.</p>
          )}
        </div>
      ) : (
        <div className="custom-scrollbar flex min-h-0 flex-1 overflow-auto font-mono text-[12px] leading-5">
          <pre
            aria-hidden="true"
            className="border-border-subtle/50 bg-surface-1/60 text-muted-foreground/60 sticky left-0 border-r px-3 py-3 text-right select-none"
          >
            {Array.from({ length: lineCount }, (_, index) => String(index + 1)).join("\n")}
          </pre>
          <pre
            className={`text-foreground min-w-0 flex-1 px-4 py-3 ${lineWrap ? "break-words whitespace-pre-wrap" : "w-max min-w-full whitespace-pre"}`}
          >
            {file.content.split("\n").map((lineText, idx) => (
              <div key={`file-line-${idx}`}>
                {lineText
                  ? renderHighlightedLineTokens(
                      lineText,
                      file.language ??
                        (file.path.split(".").pop()?.toLowerCase() || "typescript"),
                    )
                  : " "}
              </div>
            ))}
          </pre>
        </div>
      )}
    </div>
  );
}

/** Render an unsaved virtual code file, reviewable diff, HTML preview, or diagram/chart tab. */
function PreviewPanelTab({ preview }: { preview: DeepSpacePreviewRequest }) {
  const isRichVisual =
    preview.kind === "diagram" ||
    preview.kind === "chart" ||
    preview.kind === "graph" ||
    preview.kind === "card" ||
    preview.kind === "html";
  const hasDiff = Boolean(preview.diffStats?.diffLines?.length);
  const [mode, setMode] = useState<"preview" | "diff" | "code">(
    isRichVisual ? "preview" : hasDiff ? "diff" : "code",
  );
  const lang =
    preview.language ?? (preview.path?.split(".").pop()?.toLowerCase() || "typescript");

  return (
    <div
      className="flex min-h-0 flex-1 flex-col overflow-hidden"
      data-testid="deepspace-side-panel-preview"
    >
      <div className="border-border-subtle flex flex-wrap items-center gap-2 border-b px-3 py-2 text-xs">
        <FileCode size={14} className="shrink-0 text-cyan-300" />
        <span className="text-foreground min-w-0 truncate font-mono font-medium">
          {preview.path || preview.title}
        </span>
        {preview.diffStats ? (
          <span className="flex shrink-0 items-center gap-1.5 font-mono text-[11px] tabular-nums">
            <span className="text-emerald-400">+{preview.diffStats.additions}</span>
            <span className="text-rose-400">-{preview.diffStats.deletions}</span>
          </span>
        ) : null}
        <div
          className="border-border-subtle bg-surface-1 ml-auto inline-flex rounded-md border p-0.5 text-[11px]"
          role="tablist"
        >
          {isRichVisual ? (
            <button
              type="button"
              role="tab"
              aria-selected={mode === "preview"}
              onClick={() => setMode("preview")}
              className={`rounded px-2 py-0.5 font-medium transition ${
                mode === "preview"
                  ? "bg-surface-2 text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              Preview
            </button>
          ) : null}
          {hasDiff ? (
            <button
              type="button"
              role="tab"
              aria-selected={mode === "diff"}
              onClick={() => setMode("diff")}
              className={`rounded px-2 py-0.5 font-medium transition ${
                mode === "diff"
                  ? "bg-surface-2 text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              Diff
            </button>
          ) : null}
          <button
            type="button"
            role="tab"
            aria-selected={mode === "code"}
            onClick={() => setMode("code")}
            className={`rounded px-2 py-0.5 font-medium transition ${
              mode === "code"
                ? "bg-surface-2 text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {isRichVisual ? "Source" : "File"}
          </button>
        </div>
      </div>

      {mode === "preview" && preview.kind === "html" ? (
        <iframe
          title={preview.title}
          sandbox="allow-scripts"
          srcDoc={preview.content}
          className="min-h-0 w-full flex-1 border-0 bg-white"
        />
      ) : mode === "preview" && isRichVisual ? (
        <div className="custom-scrollbar min-h-0 flex-1 overflow-auto p-4">
          <DeepSpaceMarkdownRenderer
            content={`\`\`\`${preview.kind === "diagram" ? "mermaid" : preview.kind}\n${preview.content}\n\`\`\``}
          />
        </div>
      ) : mode === "diff" && preview.diffStats?.diffLines ? (
        <div
          className="custom-scrollbar min-h-0 flex-1 overflow-auto font-mono text-[11.5px] leading-5"
          data-testid="deepspace-side-panel-diff"
        >
          <table className="w-full border-collapse">
            <tbody>
              {preview.diffStats.diffLines.map((line, idx) => {
                const isAdded = line.type === "added";
                const isRemoved = line.type === "removed";
                return (
                  <tr
                    key={`${preview.id}-diff-${idx}`}
                    className={
                      isAdded
                        ? "bg-emerald-500/[0.10]"
                        : isRemoved
                          ? "bg-rose-500/[0.10]"
                          : ""
                    }
                  >
                    <td className="text-muted-foreground/50 border-border-subtle/40 w-9 border-r px-2 text-right select-none">
                      {idx + 1}
                    </td>
                    <td
                      className={`w-5 px-1.5 text-center select-none ${
                        isAdded
                          ? "text-emerald-400"
                          : isRemoved
                            ? "text-rose-400"
                            : "text-muted-foreground/40"
                      }`}
                    >
                      {line.type === "unchanged" ? " " : isAdded ? "+" : "-"}
                    </td>
                    <td className="px-3 whitespace-pre">
                      {line.text ? renderHighlightedLineTokens(line.text, lang) : " "}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="custom-scrollbar min-h-0 flex-1 overflow-auto p-3">
          <CodeBlock language={lang} value={preview.content} enableRichPreview={false} autoHeight />
        </div>
      )}
    </div>
  );
}

/** Applications returned by the host's verified launcher catalog. */
function OpenWithDropdown({
  open,
  position,
  apps,
  loading,
  error,
  onAppSelect,
  onClose,
}: {
  open: boolean;
  position: { top: number; left: number; workspaceRoot: string } | null;
  apps: Array<{ id: string; name: string; category: string }>;
  loading: boolean;
  error: string | null;
  onAppSelect: (appId: string, workspaceRoot: string) => void;
  onClose: () => void;
}) {
  if (!open || !position) return null;

  return (
    <>
      <button
        className="fixed inset-0 z-[90] cursor-default"
        aria-label="Close open-with menu"
        onClick={onClose}
      />
      <div
        style={{
          top: position.top,
          left: Math.min(position.left, Math.max(8, window.innerWidth - 280)),
        }}
        role="menu"
        aria-label="Open workspace with"
        className="border-border-subtle bg-surface-1 fixed z-[100] max-h-[min(70vh,28rem)] w-64 overflow-y-auto rounded-lg border p-1.5 text-xs shadow-xl"
      >
        <p className="text-muted-foreground px-2 py-1.5 text-[10px] font-semibold tracking-wide uppercase">
          Available on this computer
        </p>
        {loading ? <p className="text-muted-foreground px-2 py-2">Loading applications…</p> : null}
        {error ? (
          <p role="alert" className="px-2 py-2 text-rose-400">
            {error}
          </p>
        ) : null}
        {apps.map((app) => {
          const Icon =
            app.category === "terminal"
              ? SquareTerminal
              : app.category === "file-manager"
                ? FolderTree
                : FileCode;
          return (
            <button
              key={app.id}
              type="button"
              role="menuitem"
              onClick={() => onAppSelect(app.id, position.workspaceRoot)}
              className="text-foreground hover:bg-surface-2 flex w-full items-center gap-2 rounded-md px-2 py-2 text-left"
            >
              <Icon size={14} className="text-muted-foreground shrink-0" />
              <span className="min-w-0 flex-1 truncate">{app.name}</span>
              <span className="text-muted-foreground text-[9px]">{app.category}</span>
            </button>
          );
        })}
      </div>
    </>
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
  const [statusFilter, setStatusFilter] = useState<"all" | "completed" | "running" | "failed">(
    "all",
  );
  const [expandedId, setExpandedId] = useState<string | number | null>(null);
  const [copiedId, setCopiedId] = useState<string | number | null>(null);
  const [cancellingId, setCancellingId] = useState<string | number | null>(null);
  const runsRef = useRef<WorkflowRun[]>([]);

  useEffect(() => {
    runsRef.current = runs;
  }, [runs]);

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
        const snapshot = parseWorkflowRuns(data);
        setRuns((current) =>
          snapshot.map((run) => {
            const live = current.find((item) => String(item.run_id) === String(run.run_id));
            if (!live) return run;
            return {
              ...run,
              events: mergeWorkflowEvents(run.events, live.events),
            };
          }),
        );
      } catch (cause) {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : "Workflow runs could not be loaded.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, refreshToken]);

  useRealtimeEvents(
    (event) => {
      if (event.resource !== "workflow-runs") return;
      const id = event.data?.run_id;
      if (typeof id !== "string") return;
      const sequence = event.data?.sequence;
      const eventType = event.data?.event_type;
      const payload = isRecord(event.data?.payload) ? event.data.payload : null;
      const known = runsRef.current.some((run) => String(run.run_id) === id);
      setRuns((current) =>
        current.map((run) => {
          if (String(run.run_id) !== id) return run;
          const events =
            typeof sequence === "number" && typeof eventType === "string"
              ? mergeWorkflowEvents(run.events, [
                  {
                    sequence,
                    type: eventType,
                    payload: payload ?? {},
                    created_at: typeof event.occurred_at === "string" ? event.occurred_at : null,
                  },
                ])
              : run.events;
          return {
            ...run,
            status: typeof event.data?.status === "string" ? event.data.status : run.status,
            current_phase:
              typeof event.data?.current_phase === "string"
                ? event.data.current_phase
                : run.current_phase,
            stop_reason:
              typeof event.data?.stop_reason === "string"
                ? event.data.stop_reason
                : run.stop_reason,
            agents_spawned:
              typeof event.data?.agents_spawned === "number"
                ? event.data.agents_spawned
                : run.agents_spawned,
            events,
          };
        }),
      );
      // A run the panel has not seen yet still has to be picked up, so the
      // snapshot is re-read only when the live event names an unknown run.
      if (!known) setRefreshToken((value) => value + 1);
    },
    ["workflow-runs"],
  );

  useEffect(() => {
    if (
      !conversationId ||
      !runs.some((run) => ["queued", "running", "cancelling"].includes(run.status))
    )
      return;
    const timer = window.setInterval(() => setRefreshToken((value) => value + 1), 5000);
    return () => window.clearInterval(timer);
  }, [conversationId, runs]);

  const cancelRun = async (runId: string | number) => {
    if (!conversationId) return;
    setCancellingId(runId);
    try {
      const response = await fetchWithAuth(
        `/deepspace/chats/${encodeURIComponent(conversationId)}/workflow-runs/${encodeURIComponent(String(runId))}/cancel`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(`Failed to cancel workflow (${response.status})`);
      setRefreshToken((value) => value + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Workflow cancellation failed.");
    } finally {
      setCancellingId(null);
    }
  };

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
      <div className="text-muted-foreground flex h-full min-h-0 items-center justify-center p-4 text-[11px]">
        Open a conversation to see its workflow orchestration runs.
      </div>
    );
  }

  return (
    <div className="bg-surface-0 text-foreground flex h-full min-h-0 flex-col font-sans dark:bg-neutral-950">
      {/* Header bar */}
      <div className="border-border-subtle bg-surface-1/70 flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2 text-xs backdrop-blur dark:bg-neutral-900/70">
        <div className="flex items-center gap-2">
          <div className="text-foreground flex items-center gap-1.5 text-[11px] font-semibold">
            <GitBranch size={13} className="text-primary" />
            <span>Workflow Runs</span>
          </div>
          <span className="text-muted-foreground text-[10px]">({runs.length} runs)</span>
        </div>

        <div className="flex items-center gap-1.5">
          <div className="border-border-subtle bg-surface-2/60 flex items-center rounded-lg border p-0.5 text-[10px]">
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
            className="border-border-subtle bg-surface-2/60 text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-md border p-1 transition"
          >
            {loading ? (
              <AverQelLoader size={12} className="text-primary" />
            ) : (
              <RefreshCw size={12} />
            )}
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
          <div className="text-muted-foreground flex h-48 items-center justify-center gap-2 text-xs">
            <AverQelLoader size={16} className="text-primary animate-spin" />
            <span>Loading workflow runs…</span>
          </div>
        ) : filteredRuns.length === 0 ? (
          <div className="flex h-48 flex-col items-center justify-center gap-1 text-center">
            <GitBranch size={24} className="text-muted-foreground/40" />
            <p className="text-foreground text-xs font-semibold">No Workflow Runs</p>
            <p className="text-muted-foreground text-[11px]">
              {statusFilter === "all"
                ? "Fan-out delegations and multi-step pipeline scripts will appear here."
                : `No runs currently matching status "${statusFilter}".`}
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {filteredRuns.map((run) => {
              const isExpanded = expandedId === run.run_id;
              const isRunning = ["queued", "running", "cancelling"].includes(run.status);
              const isCompleted = run.status === "completed";
              const isFailed = run.status === "failed";

              let badgeColor = "border-amber-500/30 bg-amber-500/10 text-amber-400";
              if (isCompleted)
                badgeColor = "border-emerald-500/30 bg-emerald-500/10 text-emerald-400";
              else if (isFailed) badgeColor = "border-rose-500/30 bg-rose-500/10 text-rose-400";
              else if (isRunning) badgeColor = "border-cyan-500/30 bg-cyan-500/10 text-cyan-400";

              return (
                <div
                  key={run.run_id}
                  className="border-border-subtle/80 bg-surface-1/40 hover:bg-surface-1/70 rounded-lg border transition dark:bg-neutral-900/40"
                >
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setExpandedId(isExpanded ? null : run.run_id)}
                    className="flex cursor-pointer items-center justify-between gap-2.5 p-2.5 text-left"
                  >
                    <div className="flex min-w-0 flex-1 items-center gap-2">
                      <div className="bg-surface-2 text-muted-foreground rounded-md p-1">
                        <GitBranch size={13} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-foreground truncate text-[11px] font-semibold">
                            {run.workflow}
                          </span>
                          <span className="text-muted-foreground/70 truncate font-mono text-[10px]">
                            #{run.run_id}
                          </span>
                        </div>
                        <div className="text-muted-foreground mt-0.5 truncate text-[10px]">
                          {run.started_at
                            ? `Started ${new Date(run.started_at).toLocaleTimeString()}`
                            : "Pending"}
                          {run.completed_at &&
                            ` → ${new Date(run.completed_at).toLocaleTimeString()}`}
                        </div>
                        {run.current_phase && (
                          <div className="mt-0.5 truncate text-[10px] text-cyan-400">
                            Phase: {run.current_phase}
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="flex shrink-0 items-center gap-2">
                      <span
                        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-semibold capitalize ${badgeColor}`}
                      >
                        {isCompleted && <CheckCircle size={10} />}
                        {isFailed && <AlertTriangle size={10} />}
                        {isRunning && <AverQelLoader size={10} className="animate-spin" />}
                        <span>{run.status}</span>
                      </span>
                      <div className="text-muted-foreground/60">
                        {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </div>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="border-border-subtle/70 bg-surface-0/60 border-t p-3 text-xs dark:bg-neutral-950/60">
                      <div className="border-border-subtle/50 text-muted-foreground mb-2 flex items-center justify-between border-b pb-2 text-[10px]">
                        <span>
                          Run ID:{" "}
                          <strong className="text-foreground font-mono">{run.run_id}</strong>
                        </span>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            copyToClipboard(String(run.run_id), run.run_id);
                          }}
                          className="bg-surface-2 hover:text-foreground inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] transition"
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

                      <div className="text-muted-foreground mb-1 grid grid-cols-2 gap-2 text-[10px]">
                        {run.started_at && (
                          <div>
                            <span className="block text-[9px] font-semibold uppercase">
                              Started:
                            </span>
                            <span className="text-foreground">
                              {new Date(run.started_at).toLocaleString()}
                            </span>
                          </div>
                        )}
                        {run.completed_at && (
                          <div>
                            <span className="block text-[9px] font-semibold uppercase">
                              Completed:
                            </span>
                            <span className="text-foreground">
                              {new Date(run.completed_at).toLocaleString()}
                            </span>
                          </div>
                        )}
                        <div>
                          <span className="block text-[9px] font-semibold uppercase">Agents:</span>
                          <span className="text-foreground">{run.agents_spawned}</span>
                        </div>
                        {run.mode && (
                          <div>
                            <span className="block text-[9px] font-semibold uppercase">Mode:</span>
                            <span className="text-foreground">{run.mode}</span>
                          </div>
                        )}
                      </div>
                      {run.output && (
                        <div className="mb-2">
                          <span className="text-muted-foreground block text-[9px] font-semibold uppercase">
                            Result
                          </span>
                          <pre className="border-border-subtle/50 bg-surface-2/40 mt-1 max-h-40 overflow-auto rounded border p-2 text-[10px] whitespace-pre-wrap">
                            {run.output}
                          </pre>
                        </div>
                      )}
                      {run.stop_reason && (
                        <div className="text-muted-foreground mb-2 text-[10px]">
                          Stop reason: <span className="text-foreground">{run.stop_reason}</span>
                        </div>
                      )}
                      {run.error && (
                        <div className="mb-2 rounded border border-rose-500/20 bg-rose-500/10 p-2 text-[10px] text-rose-200">
                          {run.error}
                        </div>
                      )}
                      {isRunning && (
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            void cancelRun(run.run_id);
                          }}
                          disabled={cancellingId === run.run_id}
                          className="mb-2 inline-flex items-center gap-1 rounded border border-rose-500/30 px-2 py-1 text-[10px] text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
                        >
                          <X size={10} />{" "}
                          {cancellingId === run.run_id ? "Cancelling…" : "Stop workflow"}
                        </button>
                      )}
                      {run.events.length > 0 && (
                        <div className="border-border-subtle/50 bg-surface-2/40 max-h-40 space-y-1 overflow-y-auto rounded border p-2 text-[10px]">
                          {run.events.slice(-40).map((event) => (
                            <div key={`${run.run_id}-${event.sequence}`} className="flex gap-2">
                              <span className="text-muted-foreground shrink-0 font-mono">
                                {event.sequence}
                              </span>
                              <span className="shrink-0 text-cyan-400">
                                {event.type.replace("workflow/", "")}
                              </span>
                              <span className="text-foreground/80 min-w-0 break-words">
                                {typeof event.payload.message === "string"
                                  ? event.payload.message
                                  : typeof event.payload.title === "string"
                                    ? event.payload.title
                                    : ""}
                              </span>
                            </div>
                          ))}
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

/**
 * The trajectory (timing ledger) of one conversation.
 */
export function TrajectoryTab({ conversationId }: { conversationId: string | null }) {
  if (!conversationId) {
    return (
      <div className="flex h-full min-h-0 items-center justify-center px-3">
        <p className="text-muted-foreground text-[11px]">
          Open a conversation to see its trajectory.
        </p>
      </div>
    );
  }

  return <DeepSpaceTrajectoryView conversationId={conversationId} />;
}

/**
 * The deliverables (changed files & declared outputs) of one conversation.
 */
export function DeliverablesTab({
  conversationId,
  fileDiffs,
  onOpenFile,
}: {
  conversationId: string | null;
  fileDiffs?: FileDiffMap;
  onOpenFile?: (path: string) => void;
}) {
  const [data, setData] = useState<DeliverablesData>({
    changed_files: [],
    declared_deliverables: [],
  });
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
    return () => {
      cancelled = true;
    };
  }, [conversationId, refreshToken]);

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedPath(text);
      setTimeout(() => setCopiedPath(null), 2000);
    } catch {}
  };

  const query = searchQuery.trim().toLowerCase();
  const filteredChanged = (data.changed_files || []).filter(
    (f) => !query || f.path.toLowerCase().includes(query),
  );
  const filteredDeclared = (data.declared_deliverables || []).filter(
    (d) => !query || d.path.toLowerCase().includes(query),
  );

  const totalWrites = (data.changed_files || []).reduce((acc, f) => acc + (f.write_count || 1), 0);

  if (!conversationId) {
    return (
      <div className="text-muted-foreground flex h-full min-h-0 items-center justify-center p-4 text-[11px]">
        Open a conversation to see its deliverables.
      </div>
    );
  }

  return (
    <div className="bg-surface-0 text-foreground flex h-full min-h-0 flex-col font-sans dark:bg-neutral-950">
      {/* Header bar */}
      <div className="border-border-subtle bg-surface-1/70 flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2 text-xs backdrop-blur dark:bg-neutral-900/70">
        <div className="flex items-center gap-2">
          <div className="text-foreground flex items-center gap-1.5 text-[11px] font-semibold">
            <Package size={13} className="text-primary" />
            <span>Deliverables</span>
          </div>
          <span className="text-muted-foreground text-[10px]">
            ({(data.declared_deliverables || []).length} declared ·{" "}
            {(data.changed_files || []).length} modified · {totalWrites} writes)
          </span>
        </div>

        <div className="flex items-center gap-2">
          <input
            type="search"
            placeholder="Filter files…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="border-border-subtle bg-surface-0 text-foreground placeholder:text-muted-foreground focus:border-primary h-6 w-32 rounded-md border px-2 text-[10px] focus:outline-none sm:w-44 dark:bg-neutral-900"
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
            className="border-border-subtle bg-surface-2/60 text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-md border p-1 transition"
          >
            {loading ? (
              <AverQelLoader size={12} className="text-primary" />
            ) : (
              <RefreshCw size={12} />
            )}
          </button>
        </div>
      </div>

      {error && (
        <div className="m-3 rounded-lg border border-rose-300/15 bg-rose-500/10 p-2.5 text-xs text-rose-200">
          {error}
        </div>
      )}

      {/* Body List */}
      <div className="custom-scrollbar min-h-0 flex-1 space-y-4 overflow-y-auto p-3">
        {loading && data.changed_files?.length === 0 && data.declared_deliverables?.length === 0 ? (
          <div className="text-muted-foreground flex h-48 items-center justify-center gap-2 text-xs">
            <AverQelLoader size={16} className="text-primary animate-spin" />
            <span>Inspecting delivered artifacts and modified files…</span>
          </div>
        ) : filteredChanged.length === 0 && filteredDeclared.length === 0 ? (
          <div className="flex h-48 flex-col items-center justify-center gap-1 text-center">
            <Package size={24} className="text-muted-foreground/40" />
            <p className="text-foreground text-xs font-semibold">No Deliverables Yet</p>
            <p className="text-muted-foreground text-[11px]">
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
                <div className="text-primary mb-2 flex items-center gap-1.5 text-[10px] font-bold tracking-[0.16em] uppercase">
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
                        className="border-primary/30 bg-primary/5 hover:bg-primary/10 flex items-center justify-between gap-2.5 rounded-lg border p-2.5 shadow-xs transition"
                      >
                        <div className="flex min-w-0 flex-1 items-center gap-2">
                          <div className="bg-primary/15 text-primary rounded-md p-1.5">
                            <Sparkles size={14} />
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <span className="text-foreground truncate text-[12px] font-bold">
                                {fileName}
                              </span>
                              {ext && (
                                <span className="bg-primary/20 py-0.2 text-primary rounded px-1 font-mono text-[9px] font-semibold uppercase">
                                  .{ext}
                                </span>
                              )}
                            </div>
                            <p className="text-muted-foreground truncate font-mono text-[10px]">
                              {deliverable.path}
                            </p>
                          </div>
                        </div>

                        <button
                          type="button"
                          onClick={() => copyToClipboard(deliverable.path)}
                          title="Copy file path"
                          className="border-border-subtle bg-surface-1 text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-md border p-1.5 transition"
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
                <div className="text-muted-foreground mb-2 flex items-center gap-1.5 text-[10px] font-bold tracking-[0.16em] uppercase">
                  <FileCode size={12} />
                  <span>Modified Files ({filteredChanged.length})</span>
                </div>
                <div className="space-y-1">
                  {filteredChanged.map((file) => {
                    const isCode =
                      /\.(ts|tsx|js|jsx|py|json|yml|yaml|css|html|go|rs|cpp|c|sh)$/i.test(
                        file.path,
                      );
                    const FileIcon = isCode ? FileCode : FileText;
                    const fileName = file.path.split("/").pop() || file.path;

                    const diff = fileDiffs?.[file.path];
                    return (
                      <div
                        key={file.path}
                        className="group border-border-subtle/80 bg-surface-1/40 hover:bg-surface-1 hover:border-border-subtle flex items-center justify-between gap-2.5 rounded-lg border p-2 text-xs transition"
                      >
                        <button
                          type="button"
                          onClick={() => onOpenFile?.(file.path)}
                          disabled={!onOpenFile}
                          className="flex min-w-0 flex-1 items-center gap-2 text-left disabled:cursor-default"
                          title={onOpenFile ? `Open ${file.path} diff` : undefined}
                        >
                          <FileIcon size={13} className="text-muted-foreground shrink-0" />
                          <div className="min-w-0 flex-1">
                            <span className="text-foreground block truncate font-mono text-[11px] group-hover:underline">
                              {file.path}
                            </span>
                          </div>
                        </button>

                        <div className="flex shrink-0 items-center gap-2">
                          {diff ? (
                            <span className="font-mono text-[10px]">
                              <span className="text-emerald-400">+{diff.additions}</span>{" "}
                              <span className="text-rose-400">-{diff.deletions}</span>
                            </span>
                          ) : null}
                          <span className="bg-surface-2 text-muted-foreground border-border-subtle/60 rounded-full border px-1.5 py-0.5 font-mono text-[9px]">
                            {file.write_count} {file.write_count === 1 ? "write" : "writes"}
                          </span>
                          <button
                            type="button"
                            onClick={() => copyToClipboard(file.path)}
                            title="Copy file path"
                            className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded p-1 transition"
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
  /** Open loaded, workspace-scoped content in its own review tab. */
  onFileOpen?: (file: WorkspaceFile, workspaceRoot: string) => void;
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
 * a file loads its text into a separate review tab.
 */
function FilesTab({ conversationId, workspace, onFileOpen }: FilesTabProps) {
  const [directories, setDirectories] = useState<Record<string, DirectoryListing>>({});
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [fileError, setFileError] = useState<string | null>(null);
  const [loadingPath, setLoadingPath] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const [activeWorkspace, setActiveWorkspace] = useState<Workspace | null>(workspace ?? null);

  useEffect(() => {
    if (workspace) {
      setActiveWorkspace(workspace);
      setDirectories({});
      setExpandedPaths(new Set());
      setFileError(null);
      setRefreshToken((prev) => prev + 1);
    }
  }, [workspace]);
  /* eslint-enable react-hooks/set-state-in-effect */

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
    setLoadingPath(entry.path);
    setFileError(null);
    try {
      const loaded = await readWorkspaceFile(conversationId, entry.path);
      onFileOpen?.(loaded, activeWorkspace?.path || diskRoot);
    } catch (cause) {
      setFileError(errorMessage(cause, "The file could not be read."));
    } finally {
      setLoadingPath(null);
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
                  <p className="text-muted-foreground py-1.5 pl-6 text-[10px]">Empty directory</p>
                )
              ) : (
                <p className="text-muted-foreground flex items-center gap-1.5 py-1.5 pl-6 text-[10px]">
                  <AverQelLoader size={11} className="animate-spin" />
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
            disabled={loadingPath !== null}
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
  const pathCut = displayPath
    ? Math.max(displayPath.lastIndexOf("/"), displayPath.lastIndexOf("\\")) + 1
    : 0;
  const dirPart = displayPath ? displayPath.slice(0, pathCut) : "";
  const namePart = displayPath ? displayPath.slice(pathCut) : "";

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-border-subtle flex items-center gap-2 border-b px-3 py-2">
        <span
          title={displayPath || undefined}
          className="min-w-0 flex-1 cursor-default truncate font-mono text-[11px] select-text"
        >
          {dirPart ? (
            <span className="text-muted-foreground opacity-60">
              {dirPart.replace(/^\/home\/[^/]+/, "~")}
            </span>
          ) : null}
          <span className="text-foreground font-semibold">{namePart || "Workspace"}</span>
        </span>
        <button
          type="button"
          aria-label="Refresh the workspace files"
          onClick={() => setRefreshToken((token) => token + 1)}
          className="text-muted-foreground hover:bg-surface-1 hover:text-foreground shrink-0 rounded-md p-1 transition"
        >
          {rootEntries === null ? <AverQelLoader size={12} /> : <RefreshCw size={12} />}
        </button>
      </div>

      {error ? (
        <p
          role="alert"
          className="mx-3 mb-2 rounded-lg border border-rose-500/25 bg-rose-500/10 px-2 py-1.5 text-[10px] leading-4 text-rose-700 dark:text-rose-300"
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
            <AverQelLoader size={16} className="text-primary animate-spin" />
          </div>
        ) : rootEntries.length === 0 ? (
          <p className="text-muted-foreground px-2 py-4 text-[11px]">
            This conversation has no workspace files yet.
          </p>
        ) : (
          <ul className="space-y-0.5">{renderEntries(rootEntries, 0)}</ul>
        )}
      </div>

      {loadingPath ? (
        <p
          role="status"
          className="border-border-subtle text-muted-foreground border-t px-3 py-2 text-xs"
        >
          Opening {loadingPath}…
        </p>
      ) : null}
      {fileError ? (
        <p role="alert" className="border-t border-rose-500/20 px-3 py-2 text-xs text-rose-400">
          {fileError}
        </p>
      ) : null}
      {!loadingPath && !fileError ? (
        <div className="border-border-subtle/60 flex flex-1 flex-col items-center justify-center border-t px-5 text-center">
          <FileCode size={22} className="text-muted-foreground/60 mb-2" />
          <p className="text-foreground/80 text-xs font-medium">Workspace files</p>
          <p className="text-muted-foreground mt-1 max-w-xs text-[11px] leading-5">
            Select a file to open it in a review tab. Your open files stay available as tabs above.
          </p>
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

interface NewTerminalMenuProps {
  /** Shells the host can start for a new session. */
  options: TerminalShellOption[];
  /** Whether creating a session is currently unavailable. */
  disabled: boolean;
  /** Called after the user chooses which shell to start. */
  onCreate: (shell: TerminalShell) => void;
  /** Reloads the list of terminal sessions. */
  onRefresh: () => void;
  /** Whether the session list is currently loading. */
  refreshDisabled: boolean;
}

/** The pinned add-tab button for browser-style terminal session tabs. */
function NewTerminalMenu({
  options,
  disabled,
  onCreate,
  onRefresh,
  refreshDisabled,
}: NewTerminalMenuProps) {
  const [open, setOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState<{
    top: number;
    left: number;
    maxHeight: number;
    maxWidth: number;
  } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

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
      const anchor = triggerRef.current?.getBoundingClientRect();
      const menu = menuRef.current?.getBoundingClientRect();
      if (!anchor || !menu) return;
      setMenuPosition(
        positionFloatingMenu(
          anchor,
          menu,
          { width: window.innerWidth, height: window.innerHeight },
          "bottom",
        ),
      );
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
      requestAnimationFrame(() => itemRefs.current[0]?.focus());
    }
  }, [open, options.length]);

  const closeMenu = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  const focusItem = (index: number) => {
    const normalized = (index + options.length + 1) % (options.length + 1);
    itemRefs.current[normalized]?.focus();
  };

  return (
    <div ref={rootRef} className="shrink-0">
      <button
        ref={triggerRef}
        type="button"
        aria-label="Open a new terminal"
        title="Open a new terminal"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => {
          setMenuPosition(null);
          setOpen((current) => !current);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          } else if (event.key === "Escape") {
            closeMenu();
          }
        }}
        className="border-border-subtle bg-surface-1 text-foreground hover:bg-surface-2 focus-visible:ring-primary/50 inline-flex h-7 w-7 items-center justify-center rounded-lg border transition outline-none focus-visible:ring-1 disabled:cursor-not-allowed disabled:opacity-40"
      >
        <Plus size={14} />
        <span className="sr-only">New terminal</span>
      </button>
      {open && !disabled && typeof document !== "undefined"
        ? createPortal(
            <div
              ref={menuRef}
              role="menu"
              aria-label="Choose a terminal shell"
              style={{
                position: "fixed",
                top: menuPosition?.top ?? 0,
                left: menuPosition?.left ?? 0,
                minWidth: 96,
                maxHeight: menuPosition?.maxHeight,
                maxWidth: menuPosition?.maxWidth,
                overflowY: "auto",
                visibility: menuPosition ? "visible" : "hidden",
              }}
              className="border-border-subtle bg-surface-1 z-[1000] overflow-hidden rounded-md border p-1 shadow-xl"
            >
              {options.map((option, index) => (
                <button
                  key={option.id}
                  ref={(element) => {
                    itemRefs.current[index] = element;
                  }}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    onCreate(option.id);
                    closeMenu();
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "ArrowDown") {
                      event.preventDefault();
                      focusItem(index + 1);
                    } else if (event.key === "ArrowUp") {
                      event.preventDefault();
                      focusItem(index - 1);
                    } else if (event.key === "Escape") {
                      event.preventDefault();
                      closeMenu();
                    }
                  }}
                  className="text-foreground hover:bg-surface-2 block w-full rounded-sm px-2 py-1 text-left text-[11px] transition"
                >
                  New {option.label} terminal
                </button>
              ))}
              <div role="separator" className="border-border-subtle my-1 border-t" />
              <button
                ref={(element) => {
                  itemRefs.current[options.length] = element;
                }}
                type="button"
                role="menuitem"
                disabled={refreshDisabled}
                onClick={() => {
                  onRefresh();
                  closeMenu();
                }}
                onKeyDown={(event) => {
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    focusItem(options.length + 1);
                  } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    focusItem(options.length - 1);
                  } else if (event.key === "Escape") {
                    event.preventDefault();
                    closeMenu();
                  }
                }}
                className="text-foreground hover:bg-surface-2 flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left text-[11px] transition disabled:cursor-not-allowed disabled:opacity-40"
              >
                <RefreshCw size={12} />
                Refresh sessions
              </button>
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

  const createSession = async (requestedShell = shell) => {
    if (!conversationId) return;
    setCreating(true);
    try {
      const opened = await openTerminal(conversationId, requestedShell);
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
      const closedIndex = sessions.findIndex((item) => item.session_id === session.session_id);
      const remainingSessions = sessions.filter((item) => item.session_id !== session.session_id);
      setListing((current) =>
        current
          ? {
              ...current,
              sessions: current.sessions.filter((item) => item.session_id !== session.session_id),
            }
          : current,
      );
      if (activeSessionId === session.session_id) {
        const nextIndex = Math.min(Math.max(0, closedIndex), remainingSessions.length - 1);
        setActiveSessionId(remainingSessions[nextIndex]?.session_id ?? null);
      }
      setActionError(null);
    } catch (cause) {
      setActionError(errorMessage(cause, "The terminal session could not be closed."));
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-border-subtle flex min-w-0 shrink-0 items-center gap-1 border-b px-2 py-1.5">
        <div
          role="tablist"
          aria-label="Terminal sessions"
          className="scrollbar-hide flex min-w-0 flex-1 items-center gap-1 overflow-x-auto whitespace-nowrap"
        >
          {sessions.map((session) => (
            <div
              key={session.session_id}
              className={`flex shrink-0 items-center gap-0.5 rounded-lg border px-1 py-0.5 text-[11px] ${
                activeSessionId === session.session_id
                  ? "border-border-subtle bg-surface-1 text-foreground"
                  : "text-muted-foreground hover:bg-surface-1 border-transparent"
              }`}
            >
              <button
                type="button"
                role="tab"
                aria-selected={activeSessionId === session.session_id}
                onClick={() => selectSession(session.session_id)}
                title={`${session.shell} terminal`}
                className="min-w-0 truncate px-1.5 py-1 text-left"
              >
                {session.shell}
              </button>
              <button
                type="button"
                onClick={() => void closeSession(session)}
                aria-label={`Close ${session.shell} terminal tab`}
                title={`Close ${session.shell} terminal tab`}
                className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-md p-1 transition"
              >
                <X size={11} />
              </button>
            </div>
          ))}
        </div>
        <NewTerminalMenu
          options={shellOptions}
          disabled={creating || !conversationId || !shellsLoaded || !shellOptions.length}
          onCreate={(nextShell) => void createSession(nextShell)}
          onRefresh={() => setRefreshToken((token) => token + 1)}
          refreshDisabled={loading}
        />
      </div>

      {error ? (
        <p
          role="alert"
          className="mx-3 mb-2 rounded-lg border border-rose-300/15 bg-rose-300/[0.06] px-2 py-1.5 text-[10px] leading-4 text-rose-200"
        >
          {error}
        </p>
      ) : null}

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
  /** Stable native browser page key for this conversation tab. */
  viewId: string;
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
function BrowserTab({ conversationId, viewId }: BrowserTabProps) {
  const [runtime, setRuntime] = useState<BrowserRuntimeState>({
    status: "checking",
    reason: null,
    page: null,
  });

  useEffect(() => {
    if (!conversationId || window.electron?.browserView) return;
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

  if (typeof window !== "undefined" && window.electron?.browserView) {
    return <ElectronBrowser viewId={viewId} conversationId={conversationId} />;
  }
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

/** A native Chromium surface embedded in the Electron desktop panel. */
function ElectronBrowser({
  viewId,
  conversationId,
}: {
  viewId: string;
  conversationId: string | null;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const addressRef = useRef<HTMLInputElement | null>(null);
  const [urlInput, setUrlInput] = useState("");
  const [state, setState] = useState<ElectronBrowserViewState>({
    viewId,
    url: "about:blank",
    title: "",
    loading: false,
    canGoBack: false,
    canGoForward: false,
    error: null,
  });
  const [error, setError] = useState<string | null>(null);
  const bridge = typeof window === "undefined" ? undefined : window.electron?.browserView;

  useEffect(() => {
    const host = hostRef.current;
    const browser = window.electron?.browserView;
    if (!host || !browser) return;

    let mounted = true;
    let frame = 0;
    let previous = "";
    const updateBounds = () => {
      if (!mounted) return;
      const rect = host.getBoundingClientRect();
      const next = [rect.left, rect.top, rect.width, rect.height]
        .map((value) => Math.round(value))
        .join(":");
      if (next !== previous && rect.width > 0 && rect.height > 0) {
        previous = next;
        void browser
          .show(viewId, { x: rect.left, y: rect.top, width: rect.width, height: rect.height })
          .then((current) => {
            if (!mounted) return;
            setState(current);
            if (current.url !== "about:blank" && document.activeElement !== addressRef.current) {
              setUrlInput(current.url);
            }
          })
          .catch((cause: unknown) => {
            if (mounted) setError(errorMessage(cause, "The desktop browser could not be opened."));
          });
      }
      frame = window.requestAnimationFrame(updateBounds);
    };

    const syncStorage = (targetUrl: string) => {
      if (!conversationId || typeof browser.exportState !== "function") return;
      if (!targetUrl.startsWith("http://") && !targetUrl.startsWith("https://")) return;
      void browser
        .exportState(viewId)
        .then((exported) => {
          if (
            exported &&
            (exported.active_url ||
              exported.text_snapshot ||
              exported.screenshot_base64 ||
              exported.cookies.length > 0 ||
              exported.origins.length > 0)
          ) {
            return syncBrowserStorageState(conversationId, {
              ...exported,
              active_url: exported.active_url ?? targetUrl,
            });
          }
          return undefined;
        })
        .catch(() => {
          // Best-effort login sync; never block desktop browsing.
        });
    };

    const unsubscribe = browser.onState((next) => {
      if (!mounted || next.viewId !== viewId) return;
      setState(next);
      setError(next.error);
      if (next.url !== "about:blank" && document.activeElement !== addressRef.current) {
        setUrlInput(next.url);
      }
      if (!next.loading) {
        syncStorage(next.url);
      }
    });
    const observer = new ResizeObserver(updateBounds);
    observer.observe(host);
    window.addEventListener("resize", updateBounds);
    frame = window.requestAnimationFrame(updateBounds);

    return () => {
      mounted = false;
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("resize", updateBounds);
      unsubscribe();
      void browser.hide(viewId);
    };
  }, [conversationId, viewId]);

  const navigate = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const raw = urlInput.trim();
    if (!raw || !bridge) return;
    const normalized = /^(https?):\/\//i.test(raw)
      ? raw
      : /^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:\/|$)/i.test(raw)
        ? `http://${raw}`
        : `https://${raw}`;
    try {
      const parsed = new URL(normalized);
      if (
        (parsed.protocol !== "https:" && parsed.protocol !== "http:") ||
        parsed.username ||
        parsed.password
      ) {
        setError("Only HTTP and HTTPS addresses can be opened.");
        return;
      }
      setError(null);
      void bridge
        .navigate(viewId, parsed.toString())
        .then(setState)
        .catch((cause: unknown) => {
          setError(errorMessage(cause, "That page could not be opened."));
        });
    } catch {
      setError("Enter a valid website address.");
    }
  };

  const isBlank = state.url === "about:blank";
  return (
    <div className="bg-surface-0 text-foreground flex h-full min-h-0 flex-col">
      <div className="border-border-subtle bg-surface-1 flex items-center gap-1.5 border-b p-2">
        <button
          type="button"
          aria-label="Go back"
          title="Go back"
          disabled={!state.canGoBack || state.loading}
          onClick={() => {
            if (!bridge) return;
            void bridge
              .back(viewId)
              .then(setState)
              .catch((cause: unknown) => {
                setError(errorMessage(cause, "Could not go back in this page."));
              });
          }}
          className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ChevronLeft size={15} />
        </button>
        <button
          type="button"
          aria-label="Go forward"
          title="Go forward"
          disabled={!state.canGoForward || state.loading}
          onClick={() => {
            if (!bridge) return;
            void bridge
              .forward(viewId)
              .then(setState)
              .catch((cause: unknown) => {
                setError(errorMessage(cause, "Could not go forward in this page."));
              });
          }}
          className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ChevronRight size={15} />
        </button>
        <button
          type="button"
          aria-label="Reload page"
          title="Reload page"
          disabled={isBlank || state.loading}
          onClick={() => {
            if (!bridge) return;
            void bridge
              .reload(viewId)
              .then(setState)
              .catch((cause: unknown) => {
                setError(errorMessage(cause, "This page could not be reloaded."));
              });
          }}
          className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-40"
        >
          {state.loading ? <AverQelLoader size={13} /> : <RefreshCw size={14} />}
        </button>
        <form onSubmit={navigate} className="flex min-w-0 flex-1 items-center gap-1.5">
          <input
            ref={addressRef}
            aria-label="Browser URL"
            autoCapitalize="none"
            autoComplete="url"
            spellCheck={false}
            value={urlInput}
            onChange={(event) => setUrlInput(event.target.value)}
            placeholder="Search or enter website address"
            className="border-border-subtle bg-surface-0 text-foreground placeholder:text-muted-foreground/70 focus:border-primary/60 focus:ring-primary/30 min-w-0 flex-1 rounded-lg border px-3 py-1.5 text-xs outline-none focus:ring-1"
          />
          <button
            type="submit"
            disabled={!urlInput.trim() || state.loading}
            className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-lg px-3 py-1.5 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-40"
          >
            {state.loading ? <AverQelLoader size={12} /> : "Go"}
          </button>
        </form>
      </div>
      {error ? (
        <p
          role="alert"
          className="border-b border-rose-500/25 bg-rose-500/10 px-3 py-2 text-xs text-rose-700 dark:text-rose-300"
        >
          {error}
        </p>
      ) : null}
      <div className="bg-surface-0 relative min-h-0 flex-1">
        <div ref={hostRef} aria-label="Web page" className="absolute inset-0" />
        {isBlank ? (
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-2 text-center">
            <Globe size={28} className="text-muted-foreground" />
            <span className="text-muted-foreground text-xs">Search or enter a website address</span>
          </div>
        ) : null}
        {state.loading ? (
          <div className="pointer-events-none absolute inset-x-0 top-0 h-0.5 overflow-hidden">
            <div className="bg-primary h-full w-1/3 animate-pulse" />
          </div>
        ) : null}
      </div>
    </div>
  );
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

function BrowserEmptyState({ notice }: { notice?: string | null }) {
  return (
    <div className="text-foreground bg-surface-0 flex h-full flex-col items-center justify-center p-6 text-center select-none">
      <div className="border-border-subtle bg-surface-1 mb-3.5 flex h-12 w-12 items-center justify-center rounded-xl border">
        <Globe size={23} className="text-primary" />
      </div>
      <h4 className="text-sm font-semibold tracking-tight">In-App Browser</h4>
      <p className="text-muted-foreground mt-1 max-w-[19rem] text-xs leading-5">
        Enter an HTTP(S) address to start browsing
      </p>
      {notice && (
        <p
          role="status"
          className="border-primary/20 bg-primary/10 text-foreground mt-2 max-w-[20rem] rounded-lg border px-2.5 py-1 text-[11px]"
        >
          {notice}
        </p>
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
      setError("Enter a valid HTTP(S) website address, such as https://example.com.");
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
    <div className="bg-surface-0 text-foreground flex h-full min-h-0 flex-col">
      {/* Top Address & Navigation Bar */}
      <div className="border-border-subtle bg-surface-1 space-y-2 border-b p-2">
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
              className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-30"
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
              className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-30"
            >
              <ChevronRight size={14} />
            </button>
            <button
              type="button"
              aria-label="Reload the page"
              title="Reload the page"
              disabled={!loadedUrl || isBlank || busy}
              onClick={() =>
                void drive(
                  () => reloadBrowser(conversationId),
                  "The page could not be loaded again.",
                )
              }
              className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-30"
            >
              {busy ? (
                <AverQelLoader size={13} className="text-emerald-400" />
              ) : (
                <RefreshCw size={13} />
              )}
            </button>
          </div>

          <div className="relative min-w-0 flex-1">
            <input
              aria-label="Browser URL"
              value={urlInput}
              onChange={(event) => setUrlInput(event.target.value)}
              placeholder="Enter an HTTP(S) address"
              className="border-border-subtle bg-surface-0 text-foreground placeholder:text-muted-foreground/70 focus:border-primary/60 focus:ring-primary/30 w-full rounded-lg border px-3 py-1.5 text-xs transition outline-none focus:ring-1"
            />
          </div>

          <button
            type="submit"
            disabled={busy || !urlInput.trim()}
            className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-lg px-3 py-1.5 text-xs font-semibold transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? <AverQelLoader size={12} className="animate-spin" /> : "Go"}
          </button>
        </form>

        <div className="flex items-center justify-between px-1 text-[11px]">
          <label className="text-foreground/80 hover:text-foreground flex cursor-pointer items-center gap-1.5 select-none">
            <input
              type="checkbox"
              checked={allowExternal}
              onChange={(event) => setAllowExternal(event.target.checked)}
              className="sr-only"
            />
            <span
              className={`flex h-3.5 w-3.5 items-center justify-center rounded border transition ${
                allowExternal
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border-strong bg-surface-0"
              }`}
            >
              {allowExternal && <Check size={10} strokeWidth={3} />}
            </span>
            <span>Allow external sites</span>
          </label>
        </div>
      </div>

      {/* Devtools / Interaction Toolbar */}
      <div className="border-border-subtle bg-surface-1 space-y-1.5 border-b px-2.5 py-1.5">
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
            className="border-border-subtle bg-surface-0 text-foreground placeholder:text-muted-foreground/70 focus:border-primary/60 min-w-0 flex-1 rounded-lg border px-2.5 py-1 text-[11px] transition outline-none disabled:opacity-40"
          />
          <button
            type="submit"
            disabled={busy || !textInput || isBlank}
            className="border-border-subtle bg-surface-2 text-foreground hover:border-primary/50 rounded-lg border px-2.5 py-1 text-[11px] font-semibold transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Type
          </button>
        </form>

        <div className="flex flex-wrap items-center gap-1">
          <span className="text-muted-foreground mr-0.5 text-[10px] font-medium">Keys</span>
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
              className="border-border-subtle bg-surface-0 text-muted-foreground hover:text-foreground hover:bg-surface-2 rounded border px-1.5 py-0.5 font-mono text-[10px] transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
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
              className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded p-1 transition disabled:cursor-not-allowed disabled:opacity-30"
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
              className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded p-1 transition disabled:cursor-not-allowed disabled:opacity-30"
            >
              <ArrowDown size={12} />
            </button>
          </div>
        </div>
      </div>

      {error ? (
        <p
          role="alert"
          className="mx-3 my-2 rounded-lg border border-rose-500/25 bg-rose-500/10 px-2.5 py-1.5 text-xs leading-4 text-rose-700 dark:text-rose-300"
        >
          {error}
        </p>
      ) : null}

      {/* Address Sub-bar */}
      <div className="border-border-subtle bg-surface-1 text-muted-foreground flex items-center gap-2 border-b px-3 py-1.5 text-[10px]">
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full ${
              !isBlank ? "bg-primary" : "bg-muted-foreground/40"
            }`}
          />
          <span
            className="text-muted-foreground truncate font-mono"
            title={loadedUrl ?? "about:blank"}
          >
            {loadedUrl && loadedUrl !== "about:blank" ? loadedUrl : "about:blank"}
          </span>
        </div>
        {loadedUrl && loadedUrl !== "about:blank" ? (
          <a
            href={loadedUrl}
            target="_blank"
            rel="noreferrer noopener"
            className="text-primary hover:text-primary/80 flex shrink-0 items-center gap-1 font-medium transition"
          >
            <span>Open in new tab</span>
            <ExternalLink size={10} />
          </a>
        ) : null}
      </div>

      {/* Main Content Area */}
      <div className="bg-surface-0 min-h-0 flex-1 overflow-hidden">
        {!isBlank && screenshot ? (
          <button
            type="button"
            aria-label="Page preview"
            disabled={busy}
            onClick={clickPage}
            className="bg-surface-0 relative block h-full w-full cursor-default overflow-y-auto border-0 p-0 disabled:cursor-wait"
          >
            <img
              ref={screenshotRef}
              src={`data:image/png;base64,${screenshot}`}
              alt={page?.title ? `The page ${page.title}` : "The page the browser is showing"}
              draggable={false}
              className="pointer-events-none mx-auto block h-auto w-full max-w-none object-contain object-top"
            />
            {busy && (
              <div className="absolute inset-0 flex items-center justify-center bg-black/40 backdrop-blur-[1px]">
                <AverQelLoader size={24} className="animate-spin text-emerald-400" />
              </div>
            )}
          </button>
        ) : (
          <BrowserEmptyState />
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
      setError("Enter a valid HTTP(S) website address, such as https://example.com.");
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
    <div className="bg-surface-0 text-foreground flex h-full min-h-0 flex-col">
      {/* Top Address & Navigation Bar */}
      <div className="border-border-subtle bg-surface-1 space-y-2 border-b p-2">
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
              className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-30"
            >
              <ChevronLeft size={14} />
            </button>
            <button
              type="button"
              aria-label="Go forward"
              title="Go forward"
              disabled={!canGoForward}
              onClick={() =>
                setHistoryIndex((current) => Math.min(history.length - 1, current + 1))
              }
              className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-30"
            >
              <ChevronRight size={14} />
            </button>
            <button
              type="button"
              aria-label="Reload the page"
              title="Reload the page"
              disabled={!loadedUrl}
              onClick={() => setReloadToken((current) => current + 1)}
              className="text-muted-foreground hover:bg-surface-2 hover:text-foreground rounded-lg p-1.5 transition disabled:cursor-not-allowed disabled:opacity-30"
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
              className="border-border-subtle bg-surface-0 text-foreground placeholder:text-muted-foreground/70 focus:border-primary/60 focus:ring-primary/30 w-full rounded-lg border px-3 py-1.5 text-xs transition outline-none focus:ring-1"
            />
          </div>

          <button
            type="submit"
            disabled={!urlInput.trim()}
            className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-lg px-3 py-1.5 text-xs font-semibold transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Go
          </button>
        </form>

        <div className="flex items-center justify-between px-1 text-[11px]">
          <label className="text-foreground/80 hover:text-foreground flex cursor-pointer items-center gap-1.5 select-none">
            <input
              type="checkbox"
              checked={allowExternal}
              onChange={(event) => setAllowExternal(event.target.checked)}
              className="sr-only"
            />
            <span
              className={`flex h-3.5 w-3.5 items-center justify-center rounded border transition ${
                allowExternal
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border-strong bg-surface-0"
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
      <div className="border-border-subtle bg-surface-1 text-muted-foreground flex items-center gap-2 border-b px-3 py-1.5 text-[10px]">
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full ${
              loadedUrl ? "bg-primary" : "bg-muted-foreground/40"
            }`}
          />
          <span className="truncate font-mono" title={loadedUrl ?? "No page loaded"}>
            {loadedUrl ?? "No page loaded"}
          </span>
        </div>
        {loadedUrl ? (
          <a
            href={loadedUrl}
            target="_blank"
            rel="noreferrer noopener"
            className="text-primary hover:text-primary/80 flex shrink-0 items-center gap-1 font-medium transition"
          >
            <span>Open in new tab</span>
            <ExternalLink size={10} />
          </a>
        ) : null}
      </div>

      {/* Main Content Area */}
      <div className="bg-surface-0 min-h-0 flex-1 overflow-hidden">
        {loadedUrl ? (
          <iframe
            key={`${loadedUrl}#${reloadToken}`}
            title="Browser preview"
            src={loadedUrl}
            sandbox="allow-scripts allow-forms"
            className="h-full w-full border-0 bg-white"
          />
        ) : (
          <BrowserEmptyState notice={notice} />
        )}
      </div>
    </div>
  );
}
